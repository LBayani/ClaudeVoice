"""ClaudeVoice tray application (built into ClaudeVoice.exe with PyInstaller, no console window).

Starts the voice server from the project's .venv in the background, shows its state in the
system tray and offers a small menu. Exiting the tray app stops the server.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

# The exe lives in the project folder; when run as a script, use the script's folder.
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PYTHONW = ROOT / ".venv" / "Scripts" / "pythonw.exe"
LOGS = ROOT / "logs"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
NO_WINDOW = 0x08000000
EDGE = [Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe"]
STARTUP_LNK = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup\ClaudeVoice.lnk"


def read_port() -> int:
    try:   # honour server.port from config.yaml without importing the server package
        for line in (ROOT / "config.yaml").read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("port:"):
                return int(s.split(":", 1)[1].split("#")[0])
    except (OSError, ValueError):
        pass
    return 8765


PORT = read_port()
BASE = f"http://127.0.0.1:{PORT}"


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\ClaudeVoiceTray")
    return ctypes.windll.kernel32.GetLastError() != 183   # ERROR_ALREADY_EXISTS


def get_state() -> dict | None:
    try:
        with urllib.request.urlopen(BASE + "/api/state", timeout=2) as r:
            return json.loads(r.read())
    except OSError:
        return None


def server_processes() -> list[int]:
    """PIDs of running voice servers (also ones not started by us)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | "
                              "Where-Object { $_.CommandLine -match 'voice_server' } | ForEach-Object { $_.ProcessId }"],
                             capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


class Tray:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.state: dict | None = None
        self.status = "starting"
        self.base_icon = self._load_icon()
        self.icon = pystray.Icon("ClaudeVoice", self._icon("starting"), "ClaudeVoice: запуск…", self._menu())
        self.stopping = False
        self.start_time = time.time()
        self.announced = False

    # ---------- icon ----------
    def _load_icon(self) -> Image.Image:
        for p in (ROOT / "assets" / "app.png", ROOT / "assets" / "app.ico"):
            if p.exists():
                return Image.open(p).convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        img = Image.new("RGBA", (64, 64), (20, 18, 15, 255))
        ImageDraw.Draw(img).ellipse((12, 12, 52, 52), outline=(255, 179, 71, 255), width=6)
        return img

    def _icon(self, status: str) -> Image.Image:
        img = self.base_icon.copy()
        color = {"device": (90, 220, 90), "ok": (255, 179, 71), "starting": (150, 150, 150), "down": (230, 60, 50)}[status]
        d = ImageDraw.Draw(img)
        d.ellipse((40, 40, 63, 63), fill=(12, 12, 12, 255))
        d.ellipse((43, 43, 60, 60), fill=color + (255,))
        return img

    # ---------- menu ----------
    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem("Открыть настройки", self.open_settings, default=True),
            pystray.MenuItem("Мастер настройки устройства", self.open_wizard),
            pystray.MenuItem(lambda _: self._status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Перезапустить сервер", self.restart_server),
            pystray.MenuItem("Журнал", self.open_log),
            pystray.MenuItem("Папка программы", lambda: os.startfile(ROOT)),
            pystray.MenuItem("Запускать с Windows", self.toggle_autostart, checked=lambda _: STARTUP_LNK.exists()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.quit),
        )

    def _status_text(self) -> str:
        s = self.state
        if s and s.get("loading"):
            return f"Загрузка: {s.get('stage', '')}…"
        if self.status == "starting":
            return "Сервер запускается…"
        if not s:
            return "Сервер не отвечает"
        brain = {"claude": "Claude", "ollama": "Ollama", "openai": "локальный движок", "echo": "эхо"}.get(s["config"]["brain"], "?")
        dev = "устройство на связи" if s["device"]["connected"] else "устройство спит"
        return f"Мозг: {brain} · {dev}"

    # ---------- actions ----------
    def start_server(self):
        if get_state():
            return   # already running (e.g. started by hand): just adopt it
        LOGS.mkdir(exist_ok=True)
        out = open(LOGS / "server_console.log", "ab")
        env = dict(os.environ, PYTHONUNBUFFERED="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1")
        self.proc = subprocess.Popen([str(PYTHONW), "-m", "voice_server"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=out, creationflags=NO_WINDOW)
        self.status = "starting"
        self.start_time = time.time()
        self.announced = False

    def stop_server(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for pid in server_processes():
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
        self.proc = None

    def restart_server(self, *_):
        def work():
            self.status = "starting"
            self._refresh_icon()
            self.stop_server()
            time.sleep(1)
            self.start_server()
        threading.Thread(target=work, daemon=True).start()

    def open_wizard(self, *_):
        self.open_settings(page="#wizard")

    def open_settings(self, *_, page: str = ""):
        url = BASE + "/ui" + page
        edge = next((p for p in EDGE if p.exists()), None)
        if edge:
            subprocess.Popen([str(edge), f"--app={url}", f"--user-data-dir={ROOT / '.appdata' / 'edge'}",
                              "--window-size=1180,820", "--no-first-run", "--no-default-browser-check"])
        else:
            os.startfile(url)

    def open_log(self, *_):
        log = LOGS / "voice.log"
        os.startfile(log if log.exists() else LOGS)

    def toggle_autostart(self, *_):
        if STARTUP_LNK.exists():
            STARTUP_LNK.unlink()
        else:
            target = sys.executable if getattr(sys, "frozen", False) else str(PYTHONW)
            args = "" if getattr(sys, "frozen", False) else f'"{Path(__file__).resolve()}"'
            ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{STARTUP_LNK}');$s.TargetPath='{target}';"
                  f"$s.Arguments='{args}';$s.WorkingDirectory='{ROOT}';$s.IconLocation='{ROOT / 'assets' / 'app.ico'}';$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, creationflags=NO_WINDOW)
        self.icon.update_menu()

    def quit(self, *_):
        self.stopping = True
        self.icon.title = "ClaudeVoice: остановка…"
        self.stop_server()
        self.icon.stop()

    # ---------- watcher ----------
    def _refresh_icon(self):
        s = self.state
        if s and s.get("loading"):
            status = "starting"
        elif self.status == "starting" and not s:
            status = "starting"
        else:
            status = "device" if s and s["device"]["connected"] else "ok" if s else "down"
        self.icon.icon = self._icon(status)
        self.icon.title = "ClaudeVoice — " + self._status_text()
        self.icon.update_menu()

    def watch(self):
        while not self.stopping:
            self.state = get_state()
            if self.state and self.state.get("loading"):
                self.status = "starting"          # port is up, models still loading
                self.start_time = time.time()
            elif self.state:
                if self.status == "starting" and not self.announced:
                    self.announced = True
                    try:
                        self.icon.notify("Голосовой сервер запущен. Можно говорить с устройством.", "ClaudeVoice")
                    except Exception:
                        pass
                self.status = "ok"
            elif self.status == "starting":
                if time.time() - self.start_time > 240:
                    self.status = "down"
            else:
                self.status = "down"
                if self.proc is None or self.proc.poll() is not None:   # crashed or never started: bring it back
                    self.start_server()
            self._refresh_icon()
            time.sleep(3)

    def run(self):
        def setup(icon):
            icon.visible = True
            self.start_server()
            threading.Thread(target=self.watch, daemon=True).start()
        self.icon.run(setup)


def main():
    if not single_instance():
        # already running: just open the settings window
        Tray.open_settings(None)
        return
    if not PYTHONW.exists():
        ctypes.windll.user32.MessageBoxW(None, f"Не найдено окружение Python:\n{PYTHONW}\n\nЗапустите в папке программы: uv sync",
                                         "ClaudeVoice", 0x10)
        return
    Tray().run()


if __name__ == "__main__":
    main()
