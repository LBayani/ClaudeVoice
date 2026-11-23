"""ClaudeVoice PC app launcher.

  pythonw launcher.pyw               start the server if needed, open the settings window
  pythonw launcher.pyw --server-only start the server if needed (used by Windows autostart)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from voice_server.config import load_config  # noqa: E402

CFG = load_config()
BASE = f"http://127.0.0.1:{CFG.server.port}"
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
EDGE_PATHS = [Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
              Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe"]


def server_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/", timeout=1.5) as r:
            return r.status == 200
    except OSError:
        return False


def start_server():
    CFG.logs_dir.mkdir(parents=True, exist_ok=True)
    out = open(CFG.logs_dir / "server_console.log", "ab")
    env = dict(os.environ, PYTHONUNBUFFERED="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen([str(PYTHONW), "-m", "voice_server"], cwd=ROOT, env=env,
                     stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags, close_fds=True)
    out.close()   # the child has its own copy of the handle


def wait_with_splash(timeout=240) -> bool:
    """Small splash window while the models load (~20-40 s)."""
    try:
        import tkinter as tk
    except ImportError:
        tk = None
    t0 = time.time()
    if tk is None:
        while time.time() - t0 < timeout:
            if server_up():
                return True
            time.sleep(1)
        return False
    root = tk.Tk()
    root.overrideredirect(True)
    w, h = 380, 150
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 2}")
    root.configure(bg="#12110e")
    tk.Label(root, text="CLAUDEVOICE", fg="#ffb347", bg="#12110e", font=("Consolas", 16, "bold")).pack(pady=(28, 6))
    msg = tk.Label(root, text="Запускаю сервер и загружаю модели…", fg="#ece3d2", bg="#12110e", font=("Segoe UI", 10))
    msg.pack()
    dots = tk.Label(root, text="", fg="#968c7b", bg="#12110e", font=("Consolas", 10))
    dots.pack(pady=6)
    ok = {"v": False}

    def tick():
        el = int(time.time() - t0)
        dots.config(text=f"{el} с")
        if server_up():
            ok["v"] = True
            root.destroy()
        elif el > timeout:
            msg.config(text="Сервер не запустился. См. logs\\server_console.log")
            root.after(6000, root.destroy)
        else:
            root.after(1000, tick)

    root.after(300, tick)
    root.mainloop()
    return ok["v"]


def open_window():
    url = BASE + "/ui"
    edge = next((p for p in EDGE_PATHS if p.exists()), None)
    if edge:
        profile = ROOT / ".appdata" / "edge"
        subprocess.Popen([str(edge), f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,820",
                          "--no-first-run", "--no-default-browser-check"])
    else:
        import webbrowser
        webbrowser.open(url)


def main():
    if not server_up():
        start_server()
        if "--server-only" in sys.argv:
            return
        if not wait_with_splash():
            return
    elif "--server-only" in sys.argv:
        return
    open_window()


if __name__ == "__main__":
    main()
