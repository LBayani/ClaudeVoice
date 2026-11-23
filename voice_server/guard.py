"""Server-side gate for irreversible actions of Claude Code (a PreToolUse hook).

The voice prompt asks Claude to get a spoken "да" before deleting, uninstalling, shutting down and
so on, but a prompt is not a guarantee. This hook is: Claude Code runs it before every shell / file
tool call. A risky or opaque command is refused unless the user's utterance of this very turn was a
confirmation (ClaudeBrain sets CV_CONFIRMED=1 in the environment for such a turn). Pattern lists are
never complete, so anything we can't read (encoded commands, inline scripts, downloads piped to a
shell) counts as risky too.

Standalone on purpose: run as `python -I guard.py`, reads the hook JSON on stdin, exit 2 = refuse.
"""
from __future__ import annotations

import json
import os
import re
import sys

SHELL_TOOLS = ("Bash", "PowerShell")
FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

_RISKY = [
    # deleting / overwriting
    r"\brm\b", r"\brmdir\b", r"\brd\b", r"\bdel\b", r"\berase\b", r"\bunlink\b", r"\bshred\b",
    r"remove-item", r"\bri\b", r"clear-content", r"clear-item", r"\bmv\b.*\s-f", r"move-item.*-force",
    r"rmtree", r"os\.remove", r"os\.unlink", r"\.unlink\(", r"truncate", r"\bdd\b", r">\s*/dev/sd",
    r"git\s+(reset\s+--hard|clean|push\s+.*(-f|--force)|branch\s+-D|checkout\s+--\s)",
    # disks, system, power
    r"\bformat(\.com)?\s+[a-z]:", r"format-volume", r"diskpart", r"clear-disk", r"initialize-disk", r"\bmkfs", r"bcdedit", r"\bvssadmin\b",
    r"shutdown", r"stop-computer", r"restart-computer", r"logoff", r"\breboot\b", r"\bpoweroff\b",
    r"\breg(\.exe)?\s+(delete|add|import)", r"remove-itemproperty", r"set-itemproperty", r"new-itemproperty",
    r"set-executionpolicy", r"\bnetsh\b", r"set-mppreference", r"\bsc(\.exe)?\s+(delete|config|stop)",
    r"stop-service", r"\btaskkill\b", r"stop-process", r"\bkill\b", r"\bicacls\b", r"\btakeown\b",
    r"\battrib\b", r"cipher\s+/w",
    # installing / uninstalling software
    r"uninstall", r"\bwinget\b", r"\bchoco\b", r"\bscoop\b", r"msiexec", r"\bpip3?\s+(install|uninstall)",
    r"\bnpm\s+(i|install|uninstall|rm)\b", r"\bapt(-get)?\b", r"add-appxpackage", r"remove-appxpackage",
    # opaque code: we can't see what it does
    r"-e(nc(odedcommand)?)?\s+[a-z0-9+/=]{16,}", r"frombase64string", r"\biex\b", r"invoke-expression",
    r"invoke-command", r"start-process", r"\bpython[0-9.]*(\.exe)?\s+-c\b", r"\bnode(\.exe)?\s+-e\b",
    r"\bpowershell(\.exe)?\b", r"\bpwsh(\.exe)?\b", r"\bcmd(\.exe)?\s+/c", r"\bbash\s+-c", r"\beval\b",
    r"(curl|wget|iwr|invoke-webrequest|irm|invoke-restmethod)\b.*\|", r"downloadstring", r"downloadfile",
    r"\|\s*(sh|bash)\b",
]
_RISKY_RE = re.compile("|".join(f"(?:{p})" for p in _RISKY), re.IGNORECASE)

_YES = re.compile(r"^\s*(да|ага|угу|подтверждаю|конечно|давай|делай|выполняй|удаляй|можно|разрешаю|согласен|"
                  r"согласна|yes|ok|окей)\b", re.IGNORECASE)
_NO = re.compile(r"\b(нет|не надо|не нужно|не делай|стоп|отмена|отмени)\b", re.IGNORECASE)


def is_confirmation(text: str) -> bool:
    """The user's whole utterance is an explicit "yes" (short, no "нет")."""
    t = (text or "").strip()
    return bool(t) and len(t) <= 60 and bool(_YES.search(t)) and not _NO.search(t)


def risky(tool: str, tool_input: dict, cwd: str = "") -> str:
    """Why this call needs a spoken confirmation, or '' if it can run as is."""
    if tool in SHELL_TOOLS:
        cmd = str(tool_input.get("command", ""))
        m = _RISKY_RE.search(cmd)
        return f"команда может быть необратимой ({m.group(0).strip()})" if m else ""
    if tool in FILE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if not path:
            return ""
        home = os.path.normcase(os.path.abspath(cwd or os.getcwd()))
        full = os.path.normcase(os.path.abspath(os.path.join(home, path)))
        if not full.startswith(home.rstrip(os.sep) + os.sep):
            return "правка файла вне рабочей папки"
        if tool == "Write" and os.path.exists(full):
            return "перезапись существующего файла"
    return ""


def main() -> int:
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return 0
    why = risky(ev.get("tool_name", ""), ev.get("tool_input") or {}, ev.get("cwd", ""))
    if not why or os.environ.get("CV_CONFIRMED") == "1":
        return 0
    msg = (f"ClaudeVoice заблокировал действие: {why}. Сначала коротко спроси пользователя голосом, "
           "что именно сделаешь, и выполни только если его СЛЕДУЮЩАЯ реплика — явное «да».\n")
    sys.stderr.buffer.write(msg.encode("utf-8"))      # UTF-8 whatever the console code page is
    sys.stderr.flush()
    return 2


if __name__ == "__main__":
    sys.exit(main())
