"""The 'brain' turns recognized text into a reply, streamed as text chunks.

A chunk of None means "the assistant paused to work" (tool use): the session closes
the current speech so the device goes back to listening/thinking meanwhile.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Optional

from .config import ClaudeCfg

log = logging.getLogger("brain")


class PromptMixin:
    """The system prompt comes from the active character (set by Services.rebuild_brain)."""
    prompt_fn = None

    def base_prompt(self) -> str:
        return self.prompt_fn() if self.prompt_fn else self.cfg.system_prompt


class EchoBrain:
    """Test brain: repeats what it heard."""

    async def respond(self, text: str, context: str = "") -> AsyncIterator[Optional[str]]:
        yield f"Вы сказали: {text}"


_SENTENCE_END = re.compile(r"[.!?…](?=\s)|\n")


class ClaudeBrain(PromptMixin):
    """Runs Claude Code headless (`claude -p`) and keeps one conversation going via --resume."""

    def __init__(self, cfg: ClaudeCfg, mcp_url: str = ""):
        self.cfg = cfg
        self.mcp_config = json.dumps({"mcpServers": {"device": {"type": "http", "url": mcp_url}}}) \
            if (mcp_url and cfg.device_tools) else ""
        self.exe = cfg.executable or shutil.which("claude") or "claude"
        self.session_id: str | None = None
        self.last_used = 0.0
        self._lock = asyncio.Lock()   # one Claude turn at a time per conversation

    def _args(self) -> list[str]:
        a = [self.exe, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--append-system-prompt", self.base_prompt()]
        if self.cfg.model:
            a += ["--model", self.cfg.model]
        if self.cfg.permission_mode:
            a += ["--permission-mode", self.cfg.permission_mode]
        allowed = list(self.cfg.allowed_tools)
        if self.mcp_config:
            a += ["--mcp-config", self.mcp_config]
            allowed.append("mcp__device")
        if allowed:
            a += ["--allowedTools", ",".join(allowed)]
        if self.cfg.disallowed_tools:
            a += ["--disallowedTools", ",".join(self.cfg.disallowed_tools)]
        if self.cfg.max_turns:
            a += ["--max-turns", str(self.cfg.max_turns)]
        idle = time.monotonic() - self.last_used
        if self.session_id and idle < self.cfg.new_conversation_after_min * 60:
            a += ["--resume", self.session_id]
        else:
            self.session_id = None
        a += list(self.cfg.extra_args)
        return a

    async def respond(self, text: str, context: str = "") -> AsyncIterator[Optional[str]]:
        async with self._lock:
            args = self._args()
            log.info("claude <- %r (resume=%s) %s", text, self.session_id, context)
            if context:
                text = f"{context}\n\n{text}"
            t0 = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=os.path.expandvars(self.cfg.cwd) if self.cfg.cwd else None,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=16 << 20,
            )
            proc.stdin.write(text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            buf = ""
            first = True
            try:
                async for raw in proc.stdout:
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("session_id"):
                        self.session_id = ev["session_id"]
                    if ev.get("type") != "stream_event" or ev.get("parent_tool_use_id"):
                        if ev.get("type") == "result":
                            log.info("claude done in %.1fs, cost $%.3f, turns %s", time.monotonic() - t0,
                                     ev.get("total_cost_usd") or 0, ev.get("num_turns"))
                        continue
                    e = ev.get("event", {})
                    et = e.get("type")
                    if et == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                        if first:
                            log.info("claude first text after %.1fs", time.monotonic() - t0)
                            first = False
                        buf += e["delta"]["text"]
                        # emit complete sentences as soon as they are available
                        last = None
                        for m in _SENTENCE_END.finditer(buf):
                            last = m.end()
                        if last:
                            yield buf[:last]
                            buf = buf[last:]
                    elif et == "content_block_start" and e.get("content_block", {}).get("type") == "tool_use":
                        if buf.strip():
                            yield buf
                        buf = ""
                        log.info("claude uses tool: %s", e["content_block"].get("name"))
                        yield None
                    elif et == "content_block_stop" and buf.strip():
                        yield buf
                        buf = ""
                if buf.strip():
                    yield buf
                await proc.wait()
                if proc.returncode:
                    err = (await proc.stderr.read()).decode("utf-8", "replace")[-500:]
                    log.warning("claude exited %s: %s", proc.returncode, err)
                    if first:
                        yield "Не получилось связаться с Клодом."
            finally:
                self.last_used = time.monotonic()
                if proc.returncode is None:
                    log.info("claude turn cancelled, killing pid %s", proc.pid)
                    _kill_tree(proc.pid)


def _kill_tree(pid: int):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
        else:
            os.kill(pid, 9)
    except Exception as e:
        log.warning("kill %s failed: %s", pid, e)


def make_brain(name: str, claude_cfg: ClaudeCfg, mcp_url: str = "", ollama_cfg=None, devices=None, openai_cfg=None,
               pc_cfg=None):
    if name == "openai":
        from .brain_openai import OpenAIBrain
        return OpenAIBrain(openai_cfg, devices, pc_cfg)
    if name == "echo":
        return EchoBrain()
    if name == "claude":
        return ClaudeBrain(claude_cfg, mcp_url)
    if name == "ollama":
        from .brain_ollama import OllamaBrain
        return OllamaBrain(ollama_cfg, devices, pc_cfg)
    raise ValueError(f"Unknown brain: {name}")
