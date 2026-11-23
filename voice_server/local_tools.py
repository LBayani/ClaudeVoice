"""Tools for LOCAL brains (Ollama, LM Studio, ...): the device's own tools + actions on this PC.

Claude Code brings its own PC tools; local models get these instead.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import difflib
import json
import logging
import os
import re
import shutil
import string
import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from .mcp_bridge import claude_name

if TYPE_CHECKING:
    from .config import PcToolsCfg
    from .mcp_bridge import DeviceRegistry

log = logging.getLogger("pc-tools")

# Spoken names -> what to launch. Values are passed to os.startfile / ShellExecute.
KNOWN_APPS = {
    "калькулятор": "calc.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "блокнот": "notepad.exe", "notepad": "notepad.exe",
    "проводник": "explorer.exe", "explorer": "explorer.exe", "мой компьютер": "explorer.exe",
    "paint": "mspaint.exe", "пейнт": "mspaint.exe", "паинт": "mspaint.exe",
    "диспетчер задач": "taskmgr.exe", "task manager": "taskmgr.exe",
    "параметры": "ms-settings:", "настройки": "ms-settings:", "settings": "ms-settings:",
    "панель управления": "control.exe", "терминал": "wt.exe", "командная строка": "cmd.exe",
    "powershell": "powershell.exe", "ножницы": "ms-screenclip:", "камера": "microsoft.windows.camera:",
    "почта": "outlookmail:", "календарь": "outlookcal:", "магазин": "ms-windows-store:",
    "браузер": "https://www.google.com", "интернет": "https://www.google.com",
}
# Never run these through the PowerShell tool, whatever the model says. NOT a complete filter: PowerShell
# has endless ways around a pattern list, so the tool stays off unless the user turns it on knowingly.
# guard.py's wider list (deleting, installing, encoded / inline code, downloads) is applied as well.
BLOCKED_PS = [r"format-volume", r"clear-disk", r"stop-computer", r"restart-computer", r"remove-item\b.*-recurse",
              r"\brm\b.*-r", r"\bdel\b.*\/s", r"rmdir\b.*\/s", r"\bformat\b\s+[a-z]:", r"diskpart", r"bcdedit",
              r"set-executionpolicy", r"reg\s+delete", r"remove-itemproperty", r"invoke-webrequest.*\|\s*iex",
              r"\biex\b", r"invoke-expression", r"shutdown"]
# Files opened directly by voice: documents and media only. Anything else (.cpl, .msc, .hta, .wsf, .py,
# .jar, .url, ...) is shown selected in Explorer instead of being run.
DOC_EXT = {".txt", ".md", ".log", ".csv", ".pdf", ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx", ".ods",
           ".ppt", ".pptx", ".odp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".svg", ".mp3",
           ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".epub", ".djvu", ".fb2"}
EXEC_EXT = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".scr", ".com", ".lnk", ".reg"}


def _start_menu_shortcuts() -> list[Path]:
    roots = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
             Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
    out = []
    for r in roots:
        if r.exists():
            out += [p for p in r.rglob("*.lnk") if "uninstall" not in p.stem.lower() and "удал" not in p.stem.lower()]
    return out


def open_app(name: str) -> str:
    q = name.strip().lower().strip(".!?")
    target = KNOWN_APPS.get(q)
    if not target:
        for k, v in KNOWN_APPS.items():
            if k in q or q in k:
                target = v
                break
    if not target:
        links = _start_menu_shortcuts()
        names = {p.stem.lower(): p for p in links}
        exact = [p for s, p in names.items() if q in s]
        match = exact[0] if exact else names.get((difflib.get_close_matches(q, names.keys(), n=1, cutoff=0.6) or [""])[0])
        if match:
            os.startfile(str(match))
            return f"Открыл «{match.stem}»."
        return f"Не нашёл программу «{name}» в меню Пуск."
    os.startfile(target)
    return f"Открыл {name}."


def open_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", url, re.I):
            url = "https://" + url
        else:
            url = "https://www.google.com/search?q=" + url.replace(" ", "+")
    webbrowser.open(url)
    return f"Открыл {url}"


def open_folder(path: str) -> str:
    aliases = {"загрузки": "Downloads", "документы": "Documents", "рабочий стол": "Desktop",
               "изображения": "Pictures", "картинки": "Pictures", "музыка": "Music", "видео": "Videos"}
    p = path.strip()
    home = Path(os.environ.get("USERPROFILE", "~"))
    for k, v in aliases.items():
        if p.lower() == k:
            p = str(home / v)
    target = Path(os.path.expandvars(p)).expanduser()
    if not target.exists():
        return f"Путь {target} не найден."
    if target.is_file() and target.suffix.lower() in EXEC_EXT:
        return "Запускать файлы программ так нельзя: используй pc_open_app."
    if target.is_file() and target.suffix.lower() not in DOC_EXT:
        subprocess.run(["explorer.exe", f"/select,{target.resolve()}"], timeout=10)
        return f"Показал файл {target.name} в проводнике (такие файлы голосом не открываю)."
    os.startfile(str(target))
    return f"Открыл {target}"


def system_info() -> str:
    now = dt.datetime.now()
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            try:
                u = shutil.disk_usage(root)
                drives.append({"drive": letter, "free_gb": round(u.free / 1e9), "total_gb": round(u.total / 1e9)})
            except OSError:
                pass
    return json.dumps({"date": now.strftime("%d.%m.%Y"), "weekday": days[now.weekday()], "time": now.strftime("%H:%M"),
                       "drives": drives, "computer": os.environ.get("COMPUTERNAME", "")}, ensure_ascii=False)


def run_powershell(command: str) -> str:
    from . import guard
    low = command.lower()
    if any(re.search(pat, low) for pat in BLOCKED_PS) or guard.risky("PowerShell", {"command": command}):
        return ("Эта команда запрещена настройками безопасности: удаление, установка, выключение и непрозрачные "
                "команды голосом не выполняются. Скажи пользователю сделать это самому.")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "Команда не завершилась за 30 секунд."
    out = (r.stdout or "") + (("\nОшибка: " + r.stderr) if r.stderr.strip() else "")
    return (out.strip() or f"Готово (код {r.returncode}).")[:2000]


PC_TOOLS = {
    "pc_open_app": (open_app, "Открыть программу на компьютере пользователя: калькулятор, блокнот, проводник, браузер, "
                    "параметры, диспетчер задач или любую программу из меню Пуск по её названию.",
                    {"name": {"type": "string", "description": "название программы, например «калькулятор» или «Telegram»"}}),
    "pc_open_url": (open_url, "Открыть сайт в браузере. Можно передать адрес или поисковый запрос.",
                    {"url": {"type": "string", "description": "адрес сайта или поисковый запрос"}}),
    "pc_open_folder": (open_folder, "Открыть папку или документ в проводнике: «загрузки», «документы», "
                       "«рабочий стол» или полный путь.", {"path": {"type": "string"}}),
    "pc_system_info": (system_info, "Узнать дату, день недели, время и свободное место на дисках компьютера.", {}),
}
PS_TOOL = ("pc_run_powershell", run_powershell, "Выполнить команду PowerShell на компьютере пользователя и получить "
           "её вывод. Только для чтения и безопасных действий: удаление, установка программ, выключение и "
           "закодированные команды отклоняются (фильтр неполный, не пытайся его обойти).",
           {"command": {"type": "string", "description": "команда PowerShell"}})


def build_tools(pc: "PcToolsCfg", devices: "DeviceRegistry", device_tools: bool):
    """Returns (tools in OpenAI/Ollama format or None, async executor(name, args) -> str)."""
    tools, device_names, local = [], {}, {}
    if pc.enabled:
        for name, (fn, desc, props) in PC_TOOLS.items():
            local[name] = fn
            tools.append({"type": "function", "function": {"name": name, "description": desc, "parameters": {
                "type": "object", "properties": props, "required": list(props)}}})
        if pc.allow_powershell:
            name, fn, desc, props = PS_TOOL
            local[name] = fn
            tools.append({"type": "function", "function": {"name": name, "description": desc, "parameters": {
                "type": "object", "properties": props, "required": list(props)}}})
    from . import server_tools
    if device_tools:   # the body's server-side tools (inputs, music, camera) go with the device tools
        tools += server_tools.openai_tools()
    dev = devices.current() if device_tools else None
    if dev:
        for t in dev.mcp_tools or []:
            n = claude_name(t["name"])
            device_names[n] = t["name"]
            tools.append({"type": "function", "function": {
                "name": n, "description": t.get("description", "")[:1000],
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}})

    async def execute(name: str, args: dict) -> str:
        if name in local:
            try:
                res = await asyncio.to_thread(local[name], **{k: v for k, v in (args or {}).items() if isinstance(k, str)})
            except TypeError as e:
                res = f"Неверные аргументы: {e}"
            except Exception as e:
                res = f"Ошибка: {e}"
            log.info("pc tool %s(%s) -> %s", name, args, str(res)[:150])
            return str(res)
        if device_tools and name in server_tools.TOOLS:
            return await server_tools.call(name, devices, args)
        real = device_names.get(name)
        d = devices.current()
        if not real or not d:
            return f"Инструмент {name} недоступен."
        try:
            res = await d.mcp_call("tools/call", {"name": real, "arguments": args or {}})
            return " ".join(c.get("text", "") for c in res.get("content", []))
        except Exception as e:
            return f"Ошибка устройства: {e}"

    return tools or None, execute
