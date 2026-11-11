"""LLM factory, retry wrapper, and a scripted fake for tests."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from jarvis_core.config import Config
from jarvis_core.errors import LLMAuthError, LLMRateLimited, LLMUnavailable
from jarvis_core.interfaces import (
    LLM,
    ChatMsg,
    Done,
    LLMEvent,
    LLMRole,
    TextDelta,
    ToolCallEvent,
    ToolSchema,
)

log = logging.getLogger(__name__)


def build_llm(cfg: Config) -> LLM:
    from jarvis_core import secrets
    from jarvis_core.adapters.services import NIMLLM, OllamaLLM

    key = secrets.get_secret("NIM_API_KEY")
    nim = NIMLLM(cfg.llm, key)
    online = RetryingLLM(nim)
    if not cfg.llm.local_fallback:
        return online
    return FailoverLLM(online, OllamaLLM(cfg.llm), nim=nim, primary_host=cfg.llm.host, local_model=cfg.llm.local_model)


async def _reachable(host: str, timeout: float = 1.5) -> bool:
    """Can we open a connection to the online model's host at all? (No internet fails here in a moment, not after a timeout.)"""
    from urllib.parse import urlparse

    u = urlparse(host)
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, port), timeout)
        w.close()
        return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


class FailoverLLM:
    """Online models first; when they can't be reached, a model on this Mac (Ollama), and back again when they return.

    Moves only before any output has been produced (a half-spoken answer is never restarted on another model), says so
    out loud on every switch through ``notify``, and while local re-checks the internet at most every ``recheck_s``.
    """

    def __init__(self, primary: LLM, local: LLM, *, nim: object | None = None, primary_host: str = "",
                 local_model: str = "local", recheck_s: float = 45.0, reachable=None) -> None:
        self.primary = primary
        self.local = local
        self.local_model = local_model
        self.mode = "online"  # online | local
        self.recheck_s = recheck_s
        self._retry_at = 0.0
        self._nim = nim
        self._host = primary_host
        self._reachable = reachable or (lambda: _reachable(self._host))
        self.notify = None

    def set_notify(self, fn) -> None:
        """``fn(text)`` is called with a sentence to say whenever the model in use changes."""
        self.notify = fn
        if self._nim is not None:
            self._nim.notify = fn

    def _tell(self, text: str) -> None:
        log.warning("model: %s", text)
        if self.notify is not None:
            try:
                self.notify(text)
            except Exception:  # pragma: no cover
                log.debug("notice failed", exc_info=True)

    async def stream_chat(
        self,
        messages: list[ChatMsg],
        tools: list[ToolSchema] | None = None,
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        kw = {"role": role, "temperature": temperature, "max_tokens": max_tokens}
        if self.mode == "local":
            if time.monotonic() < self._retry_at or not await self._reachable():
                self._retry_at = time.monotonic() + self.recheck_s
                async for ev in self.local.stream_chat(messages, tools, **kw):
                    yield ev
                return
        emitted = False
        try:
            async for ev in self.primary.stream_chat(messages, tools, **kw):
                emitted = True
                yield ev
            if self.mode == "local":
                self.mode = "online"
                self._tell("Back online. I'm using the main model again.")
            return
        except LLMAuthError:
            raise
        except LLMUnavailable as e:
            if emitted:
                raise
            online = await self._reachable()
            if self.mode == "online":
                self.mode = "local"
                self._retry_at = time.monotonic() + self.recheck_s
                self._tell(
                    "The online models aren't responding, so I'm switching to my local model. I may be a little slower and less sharp."
                    if online else
                    "You seem to be offline, so I'm switching to my local model. I may be a little slower and less sharp until you're back."
                )
            try:
                async for ev in self.local.stream_chat(messages, tools, **kw):
                    yield ev
            except LLMUnavailable:
                log.warning("local model unavailable too (is Ollama running with %s?)", self.local_model)
                raise e from None

    async def complete(
        self,
        messages: list[ChatMsg],
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        parts: list[str] = []
        async for ev in self.stream_chat(messages, None, role=role, temperature=temperature, max_tokens=max_tokens):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
        return "".join(parts).strip()

    def list_models(self) -> list[str]:
        return self.primary.list_models()

    def health(self) -> bool:
        return self.primary.health()

    @property
    def probe(self):
        return self.primary.probe


class RetryingLLM:
    """Retries transient failures once with backoff, but only before any output."""

    def __init__(self, inner: LLM, *, retries: int = 1, backoff_s: float = 0.6) -> None:
        self.inner = inner
        self.retries = retries
        self.backoff_s = backoff_s

    async def stream_chat(
        self,
        messages: list[ChatMsg],
        tools: list[ToolSchema] | None = None,
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        attempt = 0
        while True:
            emitted = False
            try:
                async for ev in self.inner.stream_chat(
                    messages, tools, role=role, temperature=temperature, max_tokens=max_tokens
                ):
                    emitted = True
                    yield ev
                return
            except LLMAuthError:
                raise
            except LLMUnavailable as e:
                attempt += 1
                if emitted or attempt > self.retries:
                    raise
                delay = self.backoff_s * (4 if isinstance(e, LLMRateLimited) else 1) * attempt
                log.warning("LLM error (%s); retrying in %.1fs", e, delay)
                await asyncio.sleep(delay)

    async def complete(
        self,
        messages: list[ChatMsg],
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        parts: list[str] = []
        async for ev in self.stream_chat(
            messages, None, role=role, temperature=temperature, max_tokens=max_tokens
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
        return "".join(parts).strip()

    def list_models(self) -> list[str]:
        return self.inner.list_models()

    def health(self) -> bool:
        return self.inner.health()

    @property
    def probe(self):  # only exposed when the inner client can probe a single model
        return self.inner.probe


class ScriptedLLM:
    """Deterministic LLM for tests: each call consumes the next scripted reply.

    A reply is a list of events, or a plain string (shorthand for text + Done).
    """

    def __init__(self, replies: list[list[LLMEvent] | str] | None = None) -> None:
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    async def stream_chat(
        self,
        messages: list[ChatMsg],
        tools: list[ToolSchema] | None = None,
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append({"messages": messages, "tools": tools, "role": role, "temperature": temperature, "max_tokens": max_tokens})
        reply = self.replies.pop(0) if self.replies else "Very good, sir."
        if isinstance(reply, str):
            # Split into word-sized deltas to exercise streaming consumers.
            words = reply.split(" ")
            for i, w in enumerate(words):
                yield TextDelta(w + (" " if i < len(words) - 1 else ""))
            yield Done("stop")
            return
        for ev in reply:
            yield ev
        if not any(isinstance(e, Done) for e in reply):
            has_tools = any(isinstance(e, ToolCallEvent) for e in reply)
            yield Done("tool_calls" if has_tools else "stop")

    async def complete(self, messages, *, role="chat", temperature=None, max_tokens=None) -> str:
        parts = []
        async for ev in self.stream_chat(messages, None, role=role):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
        return "".join(parts).strip()

    def list_models(self) -> list[str]:
        return ["scripted"]

    def health(self) -> bool:
        return True
