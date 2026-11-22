"""Brain for any OpenAI-compatible server: LM Studio, llama.cpp / llama-swap, vLLM, KoboldCpp, Jan,
text-generation-webui, LocalAI, Ollama's /v1. Streams /chat/completions (SSE) with tool calling."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, AsyncIterator, Optional

import aiohttp

from .brain import PromptMixin
from .config import OpenAICfg
from .mcp_bridge import claude_name

if TYPE_CHECKING:
    from .mcp_bridge import DeviceRegistry

log = logging.getLogger("openai")

_SENTENCE_END = re.compile(r"[.!?…](?=\s)|\n")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
MAX_TOOL_ROUNDS = 6

PRESETS = {
    "lmstudio": ("LM Studio", "http://127.0.0.1:1234/v1"),
    "bionic": ("Bionic", "http://127.0.0.1:1235/v1"),
    "llamacpp": ("llama.cpp / llama-swap", "http://127.0.0.1:8080/v1"),
    "vllm": ("vLLM", "http://127.0.0.1:8000/v1"),
    "koboldcpp": ("KoboldCpp", "http://127.0.0.1:5001/v1"),
    "jan": ("Jan", "http://127.0.0.1:1337/v1"),
    "textgen": ("text-generation-webui", "http://127.0.0.1:5000/v1"),
    "ollama": ("Ollama (OpenAI-режим)", "http://127.0.0.1:11434/v1"),
    "custom": ("Свой адрес", ""),
}


class OpenAIBrain(PromptMixin):
    def __init__(self, cfg: OpenAICfg, devices: "DeviceRegistry", pc_cfg=None):
        self.cfg = cfg
        self.devices = devices
        self.pc = pc_cfg
        self.history: list[dict] = []
        self.last_used = 0.0
        self._lock = asyncio.Lock()
        self.ensure_fn = None          # starts the engine if needed (set by Services)
        self.engine_status = ""

    async def ensure_engine(self) -> bool:
        if not self.ensure_fn:
            return False
        ok, msg = await asyncio.to_thread(self.ensure_fn)
        self.engine_status = msg
        log.info("engine autostart: %s (%s)", "ok" if ok else "failed", msg)
        return ok

    async def warmup(self):
        """Start the engine if needed, then make it load the model now (first load can take minutes)."""
        await self.ensure_engine()
        if not self.cfg.model:
            return
        t0 = time.monotonic()
        body = {"model": self.cfg.model, "messages": [{"role": "user", "content": "привет"}],
                "max_tokens": 1, "stream": False, "reasoning_effort": "none"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
                async with s.post(self.cfg.url.rstrip("/") + "/chat/completions", json=body, headers=self._headers()) as r:
                    await r.read()
                    log.info("engine model %s ready in %.0fs (HTTP %s)", self.cfg.model, time.monotonic() - t0, r.status)
        except Exception as e:
            log.warning("engine warmup failed: %s", e)

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["Authorization"] = f"Bearer {self.cfg.api_key}"
        return h

    def _tools(self):
        from .config import PcToolsCfg
        from .local_tools import build_tools
        return build_tools(self.pc or PcToolsCfg(enabled=False), self.devices, self.cfg.device_tools)

    async def _stream(self, session, messages, tools):
        """Yields ('text', str) ... then ('done', content, tool_calls)."""
        body = {"model": self.cfg.model or "local", "messages": messages, "stream": True,
                "temperature": self.cfg.temperature, "max_tokens": self.cfg.max_tokens}
        if tools:
            body["tools"] = tools
        if self.cfg.no_think:
            # Each engine has its own switch: Ollama/LM Studio -> reasoning_effort, llama.cpp/vLLM -> template kwargs.
            body["reasoning_effort"] = "none"
            body["chat_template_kwargs"] = {"enable_thinking": False}
        # Engines differ in what they accept: retry with fewer extras instead of failing.
        attempts = [body,
                    {k: v for k, v in body.items() if k not in ("reasoning_effort", "chat_template_kwargs")},
                    {k: v for k, v in body.items() if k not in ("reasoning_effort", "chat_template_kwargs", "tools")}]
        last_err = ""
        for b in attempts:
            async with session.post(self.cfg.url.rstrip("/") + "/chat/completions", json=b,
                                    headers=self._headers()) as r:
                if r.status == 200:
                    async for item in self._parse(r):
                        yield item
                    return
                last_err = f"HTTP {r.status}: {(await r.text())[:200]}"
                if r.status not in (400, 422, 500):
                    break
                log.info("engine rejected request (%s), retrying with fewer options", last_err[:80])
        raise RuntimeError(last_err)

    @staticmethod
    async def _parse(r):
        content, calls = "", {}
        async for raw in r.content:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue
            if obj.get("error"):
                raise RuntimeError(str(obj["error"])[:200])
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content += d["content"]
                    yield ("text", d["content"])
                for tc in d.get("tool_calls") or []:          # arrive in fragments, keyed by index
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
        yield ("done", content, [calls[k] for k in sorted(calls)])

    async def respond(self, text: str, context: str = "") -> AsyncIterator[Optional[str]]:
        async with self._lock:
            from .engine_manager import reachable
            if not await asyncio.to_thread(reachable, self.cfg.url):
                if not self.ensure_fn or not self.cfg.autostart:
                    yield "Локальный движок не запущен. Запустите его или включите автозапуск в настройках."
                    return
                yield "Запускаю локальный движок, это займёт около минуты."
                if not await self.ensure_engine():
                    yield f"Не получилось запустить движок: {self.engine_status}."
                    return
            if time.monotonic() - self.last_used > self.cfg.new_conversation_after_min * 60:
                self.history = []
            user = f"{context}\n\n{text}" if context else text
            if self.cfg.no_think and "qwen" in (self.cfg.model or "").lower():
                user += " /no_think"   # Qwen3 soft switch, harmless for other Qwen builds
            from .config import LOCAL_PC_PROMPT
            system = (f"{self.base_prompt()} Ты работаешь локально на этом компьютере, на языковой модели "
                      f"{self.cfg.model or 'локальной'}, без интернета; ты не Claude и не облачный сервис."
                      + (LOCAL_PC_PROMPT if self.pc and self.pc.enabled else ""))
            messages = [{"role": "system", "content": system}] + self.history + [{"role": "user", "content": user}]
            tools, execute = self._tools()
            log.info("openai %s @ %s <- %r (history %d, tools %d)", self.cfg.model, self.cfg.url, text,
                     len(self.history), len(tools or []))
            t0, first = time.monotonic(), True
            # Long first-byte wait: the engine may still be loading a big model.
            timeout = aiohttp.ClientTimeout(total=self.cfg.max_seconds, sock_connect=5, sock_read=240)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    for _ in range(MAX_TOOL_ROUNDS):
                        buf, in_think, content, calls = "", False, "", []
                        async for item in self._stream(session, messages, tools):
                            if item[0] == "done":
                                _, content, calls = item
                                break
                            piece = item[1]
                            if "<think>" in piece:
                                in_think = True
                            if in_think:
                                if "</think>" not in piece:
                                    continue
                                in_think, piece = False, piece.split("</think>", 1)[1]
                            if first and piece.strip():
                                log.info("openai first text after %.1fs", time.monotonic() - t0)
                                first = False
                            buf += piece
                            last = None
                            for m in _SENTENCE_END.finditer(buf):
                                last = m.end()
                            if last:
                                yield buf[:last]
                                buf = buf[last:]
                        if buf.strip():
                            yield buf
                        assistant = {"role": "assistant", "content": _THINK.sub("", content).strip()}
                        if calls:
                            assistant["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                                                        "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                                                       for i, c in enumerate(calls)]
                        messages.append(assistant)
                        if not calls:
                            break
                        yield None
                        for i, c in enumerate(calls):
                            try:
                                args = json.loads(c["arguments"] or "{}")
                            except ValueError:
                                args = {}
                            result = await execute(c["name"], args)
                            log.info("openai tool %s(%s) -> %s", c["name"], args, result[:150])
                            messages.append({"role": "tool", "tool_call_id": assistant["tool_calls"][i]["id"],
                                             "content": result})
                    log.info("openai done in %.1fs", time.monotonic() - t0)
            except (aiohttp.ClientError, RuntimeError, asyncio.TimeoutError) as e:
                log.warning("openai engine failed: %s", e)
                yield "Локальный движок не отвечает. Проверьте, что он запущен и модель загружена."
                return
            finally:
                self.last_used = time.monotonic()
            self.history = messages[1:][-self.cfg.max_history:]
            while self.history and self.history[0]["role"] == "tool":
                self.history.pop(0)
