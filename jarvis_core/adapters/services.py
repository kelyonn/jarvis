"""Outside-world adapters: the NVIDIA NIM client, speech fallbacks, and web search/fetch.

Everything else in Jarvis depends on the small Protocols in :mod:`jarvis_core.interfaces`. Google sign-in lives in
:mod:`jarvis_core.adapters.google` and the Calendar/Contacts readers in :mod:`jarvis_core.adapters.apple_data`; both are
re-exported here so callers have one place to import from.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import wave
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx

from jarvis_core.config import LLMCfg
from jarvis_core.errors import LLMAuthError, LLMRateLimited, LLMUnavailable
from jarvis_core.interfaces import (
    ChatMsg,
    Done,
    LLMEvent,
    LLMRole,
    TextDelta,
    ToolCallEvent,
    ToolSchema,
    Transcript,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM: NVIDIA NIM (OpenAI-compatible)
# ---------------------------------------------------------------------------


class ToolCallAssembler:
    """Assemble OpenAI streaming ``tool_calls`` deltas into complete calls."""

    def __init__(self) -> None:
        self._calls: dict[int, dict[str, str]] = {}

    def feed(self, deltas: list[dict[str, Any]] | None) -> None:
        for d in deltas or []:
            idx = int(d.get("index", 0) or 0)
            slot = self._calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if d.get("id"):
                slot["id"] = d["id"]
            fn = d.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["arguments"] += fn["arguments"]

    def finish(self) -> list[ToolCallEvent]:
        out: list[ToolCallEvent] = []
        for idx in sorted(self._calls):
            c = self._calls[idx]
            if not c["name"]:
                continue
            raw = c["arguments"].strip() or "{}"
            try:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    args = {"value": args}
            except json.JSONDecodeError:
                log.warning("tool %s returned unparsable arguments: %r", c["name"], raw[:200])
                args = {}
            out.append(ToolCallEvent(id=c["id"] or f"call_{idx}", name=c["name"], arguments=args))
        return out


def _map_http_error(exc: Exception) -> Exception:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return LLMRateLimited("NIM rate limit reached")
        if code in (401, 403):
            return LLMAuthError(f"NIM rejected the API key (HTTP {code})")
        return LLMUnavailable(f"NIM returned HTTP {code}")
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return LLMUnavailable(f"NIM unreachable: {exc.__class__.__name__}")
    return LLMUnavailable(str(exc))


class NIMLLM:
    """:class:`jarvis_core.interfaces.LLM` for NVIDIA NIM's OpenAI-compatible streaming API."""

    def __init__(self, cfg: LLMCfg, api_key: str | None) -> None:
        self.cfg = cfg
        self.api_key = api_key
        self._working: dict[str, str] = {}  # role -> model that last answered
        self._plain: set[str] = set()  # models that rejected extra_body

    # -- setup --------------------------------------------------------------

    def _model(self, role: LLMRole) -> str:
        return {
            "chat": self.cfg.chat_model,
            "fast": self.cfg.fast_model,
            "vision": self.cfg.vision_model,
        }[role]

    def candidates(self, role: LLMRole) -> list[str]:
        """Models to try for ``role``: the one that last worked, then the configured one, then the fallbacks."""
        out: list[str] = []
        for m in [self._working.get(role), self._model(role), *self.cfg.fallback_models]:
            if m and m not in out:
                out.append(m)
        return out

    def _require_key(self) -> None:
        if not self.api_key:
            raise LLMAuthError("No NIM_API_KEY configured (run `jarvisd setup`)")

    # -- streaming ----------------------------------------------------------

    async def stream_chat(
        self,
        messages: list[ChatMsg],
        tools: list[ToolSchema] | None = None,
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a reply, moving to the next candidate model if one is gone (404/410) or unreachable.

        NIM's free catalogue lists models that no longer answer, so a dead model must not silence Jarvis.
        Fallback only happens before any output has been produced.
        """
        self._require_key()
        last: Exception | None = None
        for model in self.candidates(role):
            emitted = False
            try:
                async for ev in self._stream_model(model, messages, tools, temperature, max_tokens):
                    emitted = True
                    yield ev
                if self._working.get(role) != model:
                    if self._working.get(role) is not None or model != self._model(role):
                        log.warning("NIM %s model is now %s", role, model)
                    self._working[role] = model
                return
            except (LLMAuthError, LLMRateLimited):
                raise
            except LLMUnavailable as e:
                if emitted:
                    raise
                log.warning("NIM model %s unavailable (%s)", model, e)
                if self._working.get(role) == model:
                    self._working.pop(role, None)
                last = e
        raise last or LLMUnavailable("no NIM model available")

    async def probe(self, model: str, timeout: float = 20.0) -> tuple[bool, str]:
        """Does this exact model answer right now? (No fallback; used by ``jarvisd doctor``.)"""
        self._require_key()

        async def go() -> None:
            async for ev in self._stream_model(model, [{"role": "user", "content": "Say OK."}], None, 0.0, 40):
                if isinstance(ev, TextDelta):
                    return

        try:
            await asyncio.wait_for(go(), timeout)
            return True, "answers"
        except LLMAuthError:
            raise
        except asyncio.TimeoutError:
            return False, "timed out"
        except Exception as e:
            return False, str(e)

    async def _stream_model(self, model, messages, tools, temperature, max_tokens) -> AsyncIterator[LLMEvent]:
        """One model, with the configured ``extra_body``; models that reject it (HTTP 400) are retried without."""
        extra = {} if model in self._plain else dict(self.cfg.extra_body)
        emitted = False
        try:
            async for ev in self._stream_once(model, messages, tools, temperature, max_tokens, extra):
                emitted = True
                yield ev
            return
        except LLMUnavailable as e:
            if emitted or not extra or "HTTP 400" not in str(e):
                raise
            log.info("%s rejected extra_body; retrying without it", model)
            self._plain.add(model)
        async for ev in self._stream_once(model, messages, tools, temperature, max_tokens, {}):
            yield ev

    async def _stream_once(self, model, messages, tools, temperature, max_tokens, extra) -> AsyncIterator[LLMEvent]:
        temperature = self.cfg.temperature if temperature is None else temperature
        max_tokens = max_tokens or self.cfg.max_tokens
        asm = ToolCallAssembler()
        finish = "stop"
        try:
            chunks = self._direct_chunks(messages, tools, model, temperature, max_tokens, extra)
            async for content, tool_deltas, fin in chunks:
                if content:
                    yield TextDelta(content)
                if tool_deltas:
                    asm.feed(tool_deltas)
                if fin:
                    finish = fin
        except (httpx.HTTPError, httpx.StreamError) as e:
            raise _map_http_error(e) from e
        except Exception:
            raise
        for call in asm.finish():
            yield call
        yield Done(finish)

    async def _direct_chunks(self, messages, tools, model, temperature, max_tokens, extra):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **extra,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with httpx.AsyncClient(
            base_url=self.cfg.host, timeout=self.cfg.request_timeout_s, headers=headers
        ) as client:
            async with client.stream("POST", "/v1/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    yield delta.get("content"), delta.get("tool_calls"), choices[0].get("finish_reason")

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

    # -- discovery ----------------------------------------------------------

    def list_models(self) -> list[str]:
        if not self.api_key:
            return []
        try:
            r = httpx.get(
                f"{self.cfg.host}/v1/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10,
            )
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", [])]
        except Exception:
            return []

    def health(self) -> bool:
        if not self.api_key:
            return False
        try:
            r = httpx.get(
                f"{self.cfg.host}/v1/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=5,
            )
            return r.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Speech fallbacks (primary STT/TTS live in voice/, using mlx-whisper / kokoro)
# ---------------------------------------------------------------------------


def _pcm16_to_wav(pcm16: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return buf.getvalue()


class FasterWhisperSTT:
    """Speech-to-text fallback on ``faster-whisper`` (CPU), used when mlx-whisper is unavailable."""

    def __init__(self, model_size: str = "small") -> None:
        from faster_whisper import WhisperModel

        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> Transcript:
        import numpy as np

        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = self._model.transcribe(audio, language="en", beam_size=1)
        return Transcript(text=" ".join(seg.text.strip() for seg in segments).strip(), duration_s=info.duration)


# ---------------------------------------------------------------------------
# Calendar, contacts and Google sign-in live in their own modules; re-exported for one import point
# ---------------------------------------------------------------------------

from jarvis_core.adapters.apple_data import apple_contacts, calendar_events  # noqa: E402,F401
from jarvis_core.adapters.google import (  # noqa: E402,F401
    GMAIL_ONLY_SCOPES,
    GOOGLE_CONSOLE_URL,
    GoogleSession,
    google_extra_scopes,
    has_google_client,
    reset_google,
    run_google_oauth,
    save_google_client,
)

# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def web_search(query: str, max_results: int = 5) -> str:
    try:
        from ddgs import DDGS

        rows = DDGS().text(query, max_results=max_results)
        return "\n".join(f"{r.get('title')}: {r.get('body')} ({r.get('href')})" for r in rows)
    except Exception as e:
        return f"Search unavailable: {e}"


def fetch_url(url: str, max_chars: int = 6000) -> str:
    try:
        r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        import re

        text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", r.text)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text)[:max_chars]
    except Exception as e:
        return f"Could not fetch page: {e}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "GMAIL_ONLY_SCOPES",
    "GOOGLE_CONSOLE_URL",
    "GoogleSession",
    "NIMLLM",
    "FasterWhisperSTT",
    "ToolCallAssembler",
    "apple_contacts",
    "calendar_events",
    "fetch_url",
    "google_extra_scopes",
    "has_google_client",
    "reset_google",
    "run_google_oauth",
    "save_google_client",
    "web_search",
]
