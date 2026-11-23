"""Device tools for Claude.

The device is an MCP *server* reachable only through its WebSocket session (type:"mcp"
messages). This module exposes those tools to Claude Code as a local HTTP MCP server at /mcp.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .session import DeviceSession

log = logging.getLogger("mcp")

PROTOCOL_VERSION = "2025-06-18"
# Stock tools that do nothing visible on this firmware (the eyes UI covers the stock screen).
HIDDEN_DEVICE_TOOLS = {"self.screen.set_theme"}


def claude_name(device_name: str) -> str:
    """self.audio_speaker.set_volume -> audio_speaker_set_volume (MCP names can't contain dots)."""
    n = device_name.removeprefix("self.")
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in n)[:64]


class DeviceRegistry:
    def __init__(self):
        self._sessions: list["DeviceSession"] = []

    def add(self, s: "DeviceSession"):
        self._sessions.append(s)

    def remove(self, s: "DeviceSession"):
        if s in self._sessions:
            self._sessions.remove(s)

    def current(self) -> "DeviceSession | None":
        for s in reversed(self._sessions):
            if not s.ws.closed and s.mcp_tools is not None:
                return s
        return None


def _rpc_result(req_id, result):
    return web.json_response({"jsonrpc": "2.0", "id": req_id, "result": result})


def _rpc_error(req_id, code, message):
    return web.json_response({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def mcp_refuse_reason(request) -> str:
    """/mcp is for the local `claude` CLI only. A web page in the browser could POST here too
    (127.0.0.1 or a DNS-rebound name): it always sends Origin / Sec-Fetch-Site, and it can't send
    Content-Type: application/json cross-site without a CORS preflight, which we never grant."""
    from .settings_api import LOCAL_HOSTS, _hostname
    if request.remote not in ("127.0.0.1", "::1"):
        return "remote"
    if _hostname(request.host or "") not in LOCAL_HOSTS:
        return "host"
    if request.headers.get("Origin") or request.headers.get("Sec-Fetch-Site", "none") != "none":
        return "browser"
    if request.method == "POST" and not request.headers.get("Content-Type", "").lower().startswith("application/json"):
        return "content-type"
    return ""


async def mcp_endpoint(request: web.Request) -> web.StreamResponse:
    why = mcp_refuse_reason(request)
    if why:
        log.warning("refused /mcp from %s (%s, Origin=%s)", request.remote, why, request.headers.get("Origin"))
        raise web.HTTPForbidden()
    if request.method != "POST":
        raise web.HTTPMethodNotAllowed(request.method, ["POST"])
    registry: DeviceRegistry = request.app["devices"]
    try:
        msg = await request.json()
    except Exception:
        return _rpc_error(None, -32700, "Parse error")
    method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}

    if req_id is None:  # notification (e.g. notifications/initialized)
        return web.Response(status=202)
    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "voice-device", "version": "1.0"},
            "instructions": "Tools of the voice device the user is talking to right now "
                            "(speaker volume, screen brightness, eye color, eye emotions, status, its body inputs "
                            "and outputs, music playback, camera).",
        })
    if method == "ping":
        return _rpc_result(req_id, {})
    if method == "tools/list":
        dev = registry.current()
        from . import server_tools
        tools = server_tools.mcp_tools()
        for t in (dev.mcp_tools if dev else []):
            tools.append({"name": claude_name(t["name"]), "description": t.get("description", ""),
                          "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}}})
        return _rpc_result(req_id, {"tools": tools})
    if method == "tools/call":
        from . import server_tools
        if params.get("name") in server_tools.TOOLS:
            text = await server_tools.call(params["name"], registry, params.get("arguments") or {})
            return _rpc_result(req_id, {"content": [{"type": "text", "text": text}]})
        dev = registry.current()
        if dev is None:
            return _rpc_result(req_id, {"content": [{"type": "text", "text": "Устройство сейчас не подключено."}],
                                        "isError": True})
        wanted = params.get("name", "")
        real = next((t["name"] for t in dev.mcp_tools if claude_name(t["name"]) == wanted), None)
        if real is None:
            return _rpc_error(req_id, -32602, f"Unknown tool: {wanted}")
        try:
            result = await dev.mcp_call("tools/call", {"name": real, "arguments": params.get("arguments") or {}})
            log.info("device tool %s(%s) -> %s", real, params.get("arguments"), json.dumps(result, ensure_ascii=False)[:200])
            return _rpc_result(req_id, result)
        except Exception as e:
            log.warning("device tool %s failed: %s", real, e)
            return _rpc_result(req_id, {"content": [{"type": "text", "text": f"Ошибка устройства: {e}"}],
                                        "isError": True})
    return _rpc_error(req_id, -32601, f"Method not found: {method}")


class McpClientMixin:
    """Device-side MCP client methods, mixed into DeviceSession."""

    mcp_tools: list[dict] | None

    def _mcp_setup(self):
        self.mcp_tools = None
        self._mcp_next_id = 1
        self._mcp_pending: dict[int, asyncio.Future] = {}

    async def mcp_call(self, method: str, params: dict, timeout: float = 15.0):
        req_id = self._mcp_next_id
        self._mcp_next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._mcp_pending[req_id] = fut
        await self.send_json({"type": "mcp", "payload": {"jsonrpc": "2.0", "method": method,
                                                          "params": params, "id": req_id}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._mcp_pending.pop(req_id, None)

    def mcp_incoming(self, payload: dict):
        fut = self._mcp_pending.get(payload.get("id"))
        if fut is None or fut.done():
            return
        if "error" in payload:
            fut.set_exception(RuntimeError(payload["error"].get("message", "error")))
        else:
            fut.set_result(payload.get("result"))

    async def mcp_discover(self):
        try:
            from .camera import mcp_capabilities   # tells a board camera where to send its photos
            caps = mcp_capabilities(self.cfg, self.cfg.server.lan_host())
            await self.mcp_call("initialize", {"protocolVersion": "2024-11-05", "capabilities": caps})
            tools, cursor = [], ""
            for _ in range(10):
                res = await self.mcp_call("tools/list", {"cursor": cursor})
                tools += res.get("tools", [])
                cursor = res.get("nextCursor") or ""
                if not cursor:
                    break
            self.mcp_tools = [t for t in tools if t.get("name") not in HIDDEN_DEVICE_TOOLS]
            log.info("device tools: %s", ", ".join(t["name"] for t in tools))
        except Exception as e:
            log.warning("device MCP discovery failed: %s", e)
            self.mcp_tools = []
