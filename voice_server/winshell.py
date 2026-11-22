"""Windows shortcuts for the PC app (desktop icon, autostart). Both start ClaudeVoice.exe (tray app)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
STARTUP_LNK = STARTUP_DIR / "ClaudeVoice.lnk"
OLD_STARTUP_LNK = STARTUP_DIR / "ClaudeVoice Server.lnk"     # earlier versions
DESKTOP_LNK = Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "ClaudeVoice.lnk"
EXE = ROOT / "ClaudeVoice.exe"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
TRAY_SCRIPT = ROOT / "tray_app.py"
ICON = ROOT / "assets" / "app.ico"


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _target() -> tuple[Path, str]:
    """The tray app: the compiled exe, or the script if the exe was not built."""
    return (EXE, "") if EXE.exists() else (PYTHONW, f'"{TRAY_SCRIPT}"')


def make_shortcut(lnk: Path, description: str):
    target, args = _target()
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(lnk))});"
        f"$s.TargetPath={_ps_quote(str(target))};"
        f"$s.Arguments={_ps_quote(args)};"
        f"$s.WorkingDirectory={_ps_quote(str(ROOT))};"
        f"$s.Description={_ps_quote(description)};"
        + (f"$s.IconLocation={_ps_quote(str(ICON))};" if ICON.exists() else "")
        + "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, timeout=20,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def autostart_enabled() -> bool:
    return STARTUP_LNK.exists() or OLD_STARTUP_LNK.exists()


def set_autostart(enabled: bool):
    if OLD_STARTUP_LNK.exists():
        OLD_STARTUP_LNK.unlink()
    if enabled:
        make_shortcut(STARTUP_LNK, "ClaudeVoice: voice server in the tray")
    elif STARTUP_LNK.exists():
        STARTUP_LNK.unlink()


if __name__ == "__main__":   # uv run python -m voice_server.winshell desktop
    if "desktop" in sys.argv:
        make_shortcut(DESKTOP_LNK, "ClaudeVoice: voice assistant (tray)")
        print("Desktop shortcut:", DESKTOP_LNK, "->", _target()[0])
    if OLD_STARTUP_LNK.exists():     # migrate an old autostart entry to the tray app
        set_autostart(True)
        print("Autostart migrated to the tray app")
