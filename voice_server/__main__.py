"""Entry point: uv run python -m voice_server

The HTTP port opens immediately (settings UI works, shows "loading"); models load in the background.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiohttp import web

from .app import attach_services, build_app, load_services
from .config import load_config


async def serve(cfg):
    app = build_app(None)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.server.host, cfg.server.port).start()
    logging.info("HTTP is up on port %d, loading models...", cfg.server.port)
    from . import discovery
    app["discovery"] = {}
    await discovery.start(cfg, app["discovery"])   # devices find us on the LAN even after IP changes

    def stage(s):
        app["stage"] = s
        logging.info("loading: %s", s)

    loop = asyncio.get_running_loop()
    try:
        svc = await loop.run_in_executor(None, lambda: load_services(cfg, app["devices"], stage))
        attach_services(app, svc)
        logging.info("Ready. OTA URL for the device: http://%s:%d/xiaozhi/ota/", cfg.server.lan_host(), cfg.server.port)
        await asyncio.Event().wait()   # run until stopped
    finally:
        await runner.cleanup()


def main():
    cfg = load_config()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        handlers=([logging.StreamHandler(sys.stdout)] if sys.stdout else []) +   # pythonw: no console
                 [logging.FileHandler(cfg.logs_dir / "voice.log", encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.info("starting ClaudeVoice server")
    if not cfg.server.token:
        logging.warning("server.token is empty: any device on the LAN can connect")
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
