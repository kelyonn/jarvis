"""LLM factory, retry wrapper, and a scripted fake for tests."""

from __future__ import annotations

import asyncio
import logging
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
    from jarvis_core.adapters.services import NIMLLM

    key = secrets.get_secret("NIM_API_KEY")
    return RetryingLLM(NIMLLM(cfg.llm, key))


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
