"""LAN discovery: devices broadcast {"cv":"discover"} to UDP 8766, we answer with our OTA URL.

The answer uses the PC address on the interface the request came from, so it stays right when DHCP
hands out new addresses (to the PC, the device, or both). Each server has a persistent id; a device
pairs with the first server that answers and afterwards prefers it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import uuid

from .config import ROOT, Config

log = logging.getLogger("discovery")
PORT = 8766
ID_FILE = ROOT / "logs" / "server_id.txt"


def server_id() -> str:
    try:
        sid = ID_FILE.read_text(encoding="utf-8").strip()
        if sid:
            return sid
    except OSError:
        pass
    sid = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    ID_FILE.parent.mkdir(exist_ok=True)
    ID_FILE.write_text(sid, encoding="utf-8")
    return sid


def local_ip_for(peer: str) -> str:
    """The address of this PC on the interface that routes to `peer`."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((peer, PORT))   # UDP connect sends nothing, only picks the route
        return s.getsockname()[0]


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, cfg: Config, state: dict):
        self.cfg, self.state, self.sid = cfg, state, server_id()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(msg, dict) or msg.get("cv") != "discover":
            return
        if not self.cfg.server.discovery:
            return
        try:
            ip = local_ip_for(addr[0])
        except OSError:
            ip = self.cfg.server.lan_host()
        port = self.cfg.server.port
        reply = {"cv": "server", "id": self.sid, "name": socket.gethostname(),
                 "ota": f"http://{ip}:{port}/xiaozhi/ota/"}
        self.transport.sendto(json.dumps(reply).encode(), addr)
        self.state.update(time=time.time(), device=str(msg.get("device", ""))[:40], ip=addr[0],
                          paired=msg.get("paired") == self.sid, answered_with=ip)
        log.info("discovery: device %s at %s -> %s (paired=%s)", msg.get("device"), addr[0], ip,
                 msg.get("paired") == self.sid)


async def start(cfg: Config, state: dict):
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Responder(cfg, state), local_addr=("0.0.0.0", PORT), allow_broadcast=True)
        log.info("discovery listening on UDP %d (server id %s)", PORT, server_id())
        return transport
    except OSError as e:
        log.warning("discovery: cannot listen on UDP %d: %s", PORT, e)
        return None
