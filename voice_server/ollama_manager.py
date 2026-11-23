"""Runs a private `ollama serve` for ClaudeVoice with its own models folder and port."""
from __future__ import annotations

import logging
import os
import socket
import subprocess
from pathlib import Path

from .config import Config

log = logging.getLogger("ollama-mgr")
_proc: subprocess.Popen | None = None


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def managed_url(cfg: Config) -> str:
    return f"http://127.0.0.1:{cfg.ollama.managed_port}"


def ensure_running(cfg: Config) -> str:
    """Start the private Ollama if enabled and not running. Returns a status string."""
    global _proc
    o = cfg.ollama
    if not o.managed:
        return "disabled"
    if _port_open(o.managed_port):
        return "running"
    exe = Path(os.path.expandvars(o.executable))
    if not exe.exists():
        log.warning("ollama.exe not found at %s", exe)
        return "no-exe"
    if not o.managed_models_dir or not Path(o.managed_models_dir).exists():
        log.warning("models folder %r does not exist", o.managed_models_dir)
        return "no-models-dir"
    env = dict(os.environ, OLLAMA_MODELS=o.managed_models_dir, OLLAMA_HOST=f"127.0.0.1:{o.managed_port}")
    out = open(cfg.logs_dir / "ollama_managed.log", "ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    _proc = subprocess.Popen([str(exe), "serve"], env=env, stdin=subprocess.DEVNULL,
                             stdout=out, stderr=out, creationflags=flags)
    out.close()   # the child has its own copy of the handle
    log.info("started private Ollama (pid %s) on port %s with models from %s",
             _proc.pid, o.managed_port, o.managed_models_dir)
    return "started"


def stop():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.terminate()
        log.info("stopped private Ollama")
    _proc = None
