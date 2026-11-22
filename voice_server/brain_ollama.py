"""Local LLM brain via Ollama (/api/chat), with the device's tools exposed as function calls."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, AsyncIterator, Optional

import aiohttp

from .brain import PromptMixin
from .config import OllamaCfg
from .mcp_bridge import claude_name

if TYPE_CHECKING:
    from .mcp_bridge import DeviceRegistry

log = logging.getLogger("ollama")

_SENTENCE_END = re.compile(r"[.!?…](?=\s)|\n")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
MAX_TOOL_ROUNDS = 6


class OllamaBrain(PromptMixin):
    def __init__(self, cfg: OllamaCfg, devices: "DeviceRegistry", pc_cfg=None):
        self.cfg = cfg
        self.devices = devices
        self.pc = pc_cfg
        self.history: list[dict] = []
        self.last_used = 0.0
        self._lock = asyncio.Lock()

    async def warmup(self):
        """Load the model into VRAM ahead of the first question (slow disks take minutes)."""
        if not self.cfg.model:
            return
        t0 = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
                for _ in range(30):   # a freshly started private Ollama needs a few seconds
                    try:
                        async with s.get(f"{self.cfg.url}/api/version") as r:
                            if r.status == 200:
                                break
                    except aiohttp.ClientError:
                        pass
                    await asyncio.sleep(1)
                async with s.post(f"{self.cfg.url}/api/generate",
                                  json={"model": self.cfg.model, "keep_alive": self.cfg.keep_alive}) as r:
                    await r.read()
            log.info("ollama model %s loaded in %.0fs", self.cfg.model, time.monotonic() - t0)
        except Exception as e:
            log.warning("ollama warmup failed: %s", e)

    def _tools(self):
        from .config import PcToolsCfg
        from .local_tools import build_tools
        return build_tools(self.pc or PcToolsCfg(enabled=False), self.devices, self.cfg.device_tools)

    async def _chat(self, session: aiohttp.ClientSession, messages, tools):
        """Stream one assistant turn. Yields ('text', str) and finally ('done', content, tool_calls)."""
        # Hard cap on length: small models sometimes never stop (runaway "thinking" or loops).
        limit = self.cfg.max_tokens * (4 if self.cfg.think else 1)
        body = {"model": self.cfg.model, "messages": messages, "stream": True,
                "keep_alive": self.cfg.keep_alive,
                "options": {"temperature": self.cfg.temperature, "num_predict": limit, "repeat_penalty": 1.1}}
        if tools:
            body["tools"] = tools
        if not self.cfg.think:
            body["think"] = False
        async with session.post(f"{self.cfg.url}/api/chat", json=body) as r:
            if r.status == 400 and "think" in body:          # model without thinking support
                body.pop("think")
                async for item in self._chat_body(session, body):
                    yield item
                return
            if r.status != 200:
                raise RuntimeError(f"Ollama HTTP {r.status}: {(await r.text())[:200]}")
            async for item in self._parse(r):
                yield item

    async def _chat_body(self, session, body):
        async with session.post(f"{self.cfg.url}/api/chat", json=body) as r:
            if r.status != 200:
                raise RuntimeError(f"Ollama HTTP {r.status}: {(await r.text())[:200]}")
            async for item in self._parse(r):
                yield item

    @staticmethod
    async def _parse(r):
        content, calls = "", []
        async for raw in r.content:
            if not raw.strip():
                continue
            obj = json.loads(raw)
            if obj.get("error"):
                raise RuntimeError(obj["error"])
            msg = obj.get("message") or {}
            if msg.get("content"):
                content += msg["content"]
                yield ("text", msg["content"])
            calls += msg.get("tool_calls") or []
            if obj.get("done"):
                break
        yield ("done", content, calls)

    async def respond(self, text: str, context: str = "") -> AsyncIterator[Optional[str]]:
        if not self.cfg.model:
            yield "Локальная модель не выбрана. Выберите её в приложении настроек."
            return
        async with self._lock:
            if time.monotonic() - self.last_used > self.cfg.new_conversation_after_min * 60:
                self.history = []
            user = f"{context}\n\n{text}" if context else text
            if not self.cfg.think:
                user += " /no_think"   # Qwen3 soft switch; some models ignore the API flag
            from .config import LOCAL_PC_PROMPT
            system = (f"{self.base_prompt()} Ты работаешь локально на этом компьютере, на языковой модели "
                      f"{self.cfg.model} через Ollama, без интернета; ты не Claude и не облачный сервис."
                      + (LOCAL_PC_PROMPT if self.pc and self.pc.enabled else ""))
            messages = [{"role": "system", "content": system}] + self.history + \
                       [{"role": "user", "content": user}]
            tools, execute = self._tools()
            log.info("ollama %s <- %r (history %d, tools %d)", self.cfg.model, text,
                     len(self.history), len(tools or []))
            t0 = time.monotonic()
            first = True
            timeout = aiohttp.ClientTimeout(total=self.cfg.max_seconds, sock_connect=5, sock_read=90)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    for _ in range(MAX_TOOL_ROUNDS):
                        buf, in_think = "", False
                        content, calls = "", []
                        async for item in self._chat(session, messages, tools):
                            if item[0] == "done":
                                _, content, calls = item
                                break
                            piece = item[1]
                            # Some models still emit <think>...</think> inline: never speak it.
                            if "<think>" in piece:
                                in_think = True
                            if in_think:
                                if "</think>" in piece:
                                    in_think = False
                                    piece = piece.split("</think>", 1)[1]
                                else:
                                    continue
                            if first and piece.strip():
                                log.info("ollama first text after %.1fs", time.monotonic() - t0)
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
                        content = _THINK.sub("", content).strip()
                        assistant = {"role": "assistant", "content": content}
                        if calls:
                            assistant["tool_calls"] = calls
                        messages.append(assistant)
                        if not calls:
                            break
                        yield None   # working: let the device listen / show "thinking"
                        for call in calls:
                            fn = call.get("function", {})
                            name, args = fn.get("name", ""), fn.get("arguments") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except ValueError:
                                    args = {}
                            result = await execute(name, args)
                            log.info("ollama tool %s(%s) -> %s", name, args, result[:150])
                            messages.append({"role": "tool", "content": result, "tool_name": name})
                    log.info("ollama done in %.1fs", time.monotonic() - t0)
            except (aiohttp.ClientError, RuntimeError, asyncio.TimeoutError) as e:
                log.warning("ollama failed: %s", e)
                yield "Локальная модель не отвечает. Проверьте, что Ollama запущена."
                return
            finally:
                self.last_used = time.monotonic()
            # keep the conversation (without the system prompt), bounded
            self.history = messages[1:][-self.cfg.max_history:]
            while self.history and self.history[0]["role"] == "tool":
                self.history.pop(0)
