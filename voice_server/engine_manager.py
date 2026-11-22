"""Starts the selected OpenAI-compatible engine when it is not running.

- LM Studio and Bionic (built on LM Studio): `lms server start --port N` runs the server headless.
  Windows reserves random port ranges at every boot (Hyper-V / WinNAT, see `netsh int ipv4 show
  excludedportrange`); if the configured port falls into one, we pick a free port outside them
  and update the engine URL.
- Any other engine: an optional user command (`openai.start_command`), e.g. llama-swap.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import Config

log = logging.getLogger("engine-mgr")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LMS_PRESETS = {"lmstudio", "bionic"}


def _lms_path() -> Path | None:
    for p in (Path(os.environ.get("USERPROFILE", "")) / ".lmstudio" / "bin" / "lms.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Bionic" / "resources" / "app" / ".webpack-bionic" / "lms.exe"):
        if p.exists():
            return p
    w = shutil.which("lms")
    return Path(w) if w else None


def reachable(url: str, timeout: float = 1.5) -> bool:
    u = urlparse(url)
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=timeout):
            return True
    except OSError:
        return False


def excluded_ranges() -> list[tuple[int, int]]:
    try:
        out = subprocess.run(["netsh", "int", "ipv4", "show", "excludedportrange", "protocol=tcp"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
    except Exception:
        return []
    return [(int(a), int(b)) for a, b in re.findall(r"^\s*(\d+)\s+(\d+)", out, re.M)]


def port_usable(port: int, ranges=None) -> bool:
    ranges = excluded_ranges() if ranges is None else ranges
    if any(a <= port <= b for a, b in ranges):
        return False
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int) -> int:
    ranges = excluded_ranges()
    for p in [preferred, 41234, 41235, 31234, 21234] + list(range(41240, 41300)):
        if port_usable(p, ranges):
            return p
    return preferred


def _with_port(url: str, port: int) -> str:
    u = urlparse(url)
    return u._replace(netloc=f"{u.hostname or '127.0.0.1'}:{port}").geturl()


def ensure_engine(cfg: Config) -> tuple[bool, str]:
    """Make the configured engine reachable. Returns (ok, message). May change cfg.openai.url."""
    o = cfg.openai
    if reachable(o.url):
        return True, "уже работает"
    if not o.autostart:
        return False, "автозапуск выключен"
    if o.start_command.strip():
        log.info("starting engine: %s", o.start_command)
        subprocess.Popen(o.start_command, shell=True, cwd=o.start_cwd or None, creationflags=NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _wait(o.url, 90), "запущен своей командой"
    local = (urlparse(o.url).hostname or "") in ("localhost", "127.0.0.1", "::1")
    # "Свой адрес" pointing at this PC with LM Studio/Bionic installed is almost always one of them.
    if o.preset in LMS_PRESETS or (o.preset == "custom" and local and _lms_path()):
        lms = _lms_path()
        if not lms:
            return False, "не найден lms.exe (LM Studio / Bionic)"
        port = urlparse(o.url).port or 1234
        if not port_usable(port):
            new = pick_port(port)
            log.info("port %s is reserved by Windows or busy -> using %s", port, new)
            o.url = _with_port(o.url, new)
            port = new
            from .config import apply_settings
            apply_settings(cfg, {"openai": {"url": o.url}})
        log.info("starting %s server on port %s", o.preset, port)
        r = subprocess.run([str(lms), "server", "start", "--port", str(port)], capture_output=True, text=True,
                           timeout=120, creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        if r.returncode != 0 and not reachable(o.url):
            msg = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
            return False, f"lms: {msg[0][:150]}"
        ok = _wait(o.url, 30)
        if ok and o.model:   # make sure the model is loaded (JIT may be disabled in the engine)
            subprocess.run([str(lms), "load", o.model, "-y"], capture_output=True, timeout=600,
                           creationflags=NO_WINDOW)
        return ok, f"сервер запущен на порту {port}"
    return False, "для этого движка не задана команда запуска"


def _wait(url: str, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if reachable(url):
            return True
        time.sleep(1)
    return False
