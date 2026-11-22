"""Tools that live on the server but belong to the device's "body": they are offered to every brain
(Claude via /mcp, local models via local_tools) next to the device's own MCP tools.

Each tool: name -> (async fn(devices, **args) -> str, description, JSON-schema properties, required).
Other modules (music, camera) register theirs with `register()`.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from . import hardware

if TYPE_CHECKING:
    from .mcp_bridge import DeviceRegistry

log = logging.getLogger("server-tools")

ToolFn = Callable[..., Awaitable[str]]
TOOLS: dict[str, tuple[ToolFn, str, dict, list]] = {}


def register(name: str, fn: ToolFn, description: str, props: dict | None = None, required: list | None = None):
    TOOLS[name] = (fn, description, props or {}, required if required is not None else list(props or {}))


async def call(name: str, devices: "DeviceRegistry", args: dict) -> str:
    fn = TOOLS[name][0]
    try:
        res = await fn(devices, **{k: v for k, v in (args or {}).items() if isinstance(k, str)})
    except TypeError as e:
        res = f"Неверные аргументы: {e}"
    except (ValueError, RuntimeError) as e:
        res = f"Ошибка: {e}"
    log.info("server tool %s(%s) -> %s", name, args, str(res)[:150])
    return str(res)


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": {
        "type": "object", "properties": p, "required": r}}} for n, (_, d, p, r) in TOOLS.items()]


def mcp_tools() -> list[dict]:
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}}
            for n, (_, d, p, r) in TOOLS.items()]


async def push_hardware(devices: "DeviceRegistry") -> str:
    """Ask the connected device to download the profile again."""
    dev = devices.current()
    if dev is None or "self.hw.reload" not in [t["name"] for t in (dev.mcp_tools or [])]:
        return "устройство применит это при следующем включении"
    try:
        res = await dev.mcp_call("tools/call", {"name": "self.hw.reload", "arguments": {}}, timeout=8)
        return " ".join(c.get("text", "") for c in (res or {}).get("content", []))
    except Exception as e:
        return f"устройство не ответило: {e}"


# ---------------- body: inputs (antennae, buttons) ----------------
def _device_profile(devices) -> tuple[str, dict]:
    dev = devices.current()
    device = dev.device_id if dev is not None and dev.device_id not in ("", "?") else ""
    return device, (hardware.load(device) if device else None) or hardware.load("")


async def body_inputs(devices) -> str:
    _, hw = _device_profile(devices)
    return json.dumps({"inputs": hw["inputs"], "outputs": hw["outputs"],
                       "available_actions": hardware.ACTIONS}, ensure_ascii=False)


async def body_set_input_action(devices, input: str, gesture: str, actions: str) -> str:
    device, hw = _device_profile(devices)
    if gesture not in hardware.GESTURES:
        raise ValueError("gesture: tap, double или hold")
    target = next((i for i in hw["inputs"] if i["name"].lower() == str(input).lower()
                   or str(i["pin"]) == str(input).removeprefix("GPIO")), None)
    if target is None:
        raise ValueError("нет такого входа; есть: " + ", ".join(i["name"] for i in hw["inputs"]))
    target[gesture] = actions
    hardware.save(hw, device)   # validates the action names
    applied = await push_hardware(devices)
    return f"«{target['name']}», жест {gesture}: {actions}. {applied}"


register("body_inputs", body_inputs,
         "Список входов тела устройства (сенсорные антенки, кнопки), что делает каждый жест (tap/double/hold), "
         "выходы (вибромотор, светодиоды) и все доступные действия.")
register("body_set_input_action", body_set_input_action,
         "Назначить действие на жест входа устройства. `input`: имя входа (например «Левая антенна») или номер "
         "GPIO. `gesture`: tap (касание), double (двойное), hold (удержание). `actions`: одно или несколько "
         "действий через запятую из списка body_inputs, например `affection,vibrate` или `listen` "
         "(рация: слушать, пока держат) или `event` (сообщить тебе, чтобы ты сам решил, как реагировать).",
         {"input": {"type": "string"}, "gesture": {"type": "string", "enum": list(hardware.GESTURES)},
          "actions": {"type": "string"}})
