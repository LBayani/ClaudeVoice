"""Which devices get the WebSocket token.

The OTA endpoint is open to the whole LAN (the device has no secret before it is paired), so the
token goes only to devices the user knows: ones with a hardware profile (set up by the wizard or
in «Железо»), ones approved in the app, and the very first device of a fresh install (trust on
first use). Any other device is listed as "waiting" in the app until the user allows it.

Device-Id is the board's MAC, visible to anyone on the same network, so this stops strangers and
rogue gadgets, not a determined attacker inside the LAN (see README, «Модель угроз»).
"""
from __future__ import annotations

import json
import logging
import time

from .config import ROOT, atomic_write_text

log = logging.getLogger("pairing")
FILE = ROOT / "logs" / "known_devices.json"
_pending: dict[str, dict] = {}


def _key(device: str) -> str:
    return "".join(c for c in (device or "").lower() if c in "0123456789abcdef")


def _fmt(key: str) -> str:
    return ":".join(key[i:i + 2] for i in range(0, len(key), 2))


def _stored() -> set[str]:
    try:
        return {_key(d) for d in json.loads(FILE.read_text(encoding="utf-8")) if _key(d)}
    except (OSError, ValueError, TypeError):
        return set()


def known() -> set[str]:
    from . import hardware
    return _stored() | {_key(d["device"]) for d in hardware.devices()}


def allow(device: str):
    k = _key(device)
    if not k:
        return
    _pending.pop(k, None)
    keys = _stored() | {k}
    FILE.parent.mkdir(exist_ok=True)
    atomic_write_text(FILE, json.dumps(sorted(_fmt(x) for x in keys), indent=1))
    log.info("device %s allowed", _fmt(k))


def forget(device: str):
    k = _key(device)
    _pending.pop(k, None)
    keys = _stored() - {k}
    if FILE.exists():
        atomic_write_text(FILE, json.dumps(sorted(_fmt(x) for x in keys), indent=1))


def migrate(last_device: str):
    """First start of a version with pairing: the board that already talks to this server stays trusted
    (its Device-Id may differ from the MAC its hardware profile was saved under)."""
    if FILE.exists() or not _key(last_device) or _key(last_device) == _key("fa:ke:00:00:00:01"):
        return
    allow(last_device)
    log.info("pairing introduced: %s (the last connected device) is trusted", last_device)


def check(device: str, ip: str = "", board: str = "", last_device: str = "") -> bool:
    """True if this device may have the token. Unknown devices are remembered as waiting."""
    k = _key(device)
    if not k:
        return False
    kn = known()
    if k in kn:
        return True
    if not kn and (not last_device or _key(last_device) == k):   # fresh install / upgrade: trust the first board
        allow(device)
        return True
    _pending[k] = {"device": _fmt(k), "ip": ip, "board": board, "time": time.time()}
    log.warning("device %s at %s is not paired: waiting for the user's OK in the app", _fmt(k), ip)
    return False


def pending() -> list[dict]:
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["time"] > 3600]:
        del _pending[k]
    return list(_pending.values())
