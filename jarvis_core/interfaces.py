"""Small Protocols so the rest of Jarvis never sees any vendor's types."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

# OpenAI-format chat message: {"role": ..., "content": ..., "tool_calls": ..., "tool_call_id": ...}
ChatMsg = dict[str, Any]
# OpenAI-format tool schema: {"type": "function", "function": {name, description, parameters}}
ToolSchema = dict[str, Any]

LLMRole = Literal["chat", "fast", "vision"]


@dataclass(slots=True)
class TextDelta:
    text: str


@dataclass(slots=True)
class ToolCallEvent:
    """A fully assembled tool call (arguments already parsed)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Done:
    finish_reason: str = "stop"


LLMEvent = TextDelta | ToolCallEvent | Done


@dataclass(slots=True)
class Transcript:
    text: str
    duration_s: float = 0.0
    language: str = "en"
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


@dataclass(slots=True)
class AudioChunk:
    pcm16: bytes
    sample_rate: int = 24000


@dataclass(slots=True)
class Fact:
    id: str = ""
    kind: str = "fact"  # fact | person | preference | style | loop
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    confidence: float = 1.0


@dataclass(slots=True)
class Item:
    """A normalised inbound item (message, email, event) from any connector."""

    source: str  # whatsapp | imessage | gmail | linkedin | calendar | ...
    id: str
    sender: str = ""
    sender_id: str = ""
    title: str = ""
    body: str = ""
    timestamp: datetime | None = None
    thread_id: str = ""
    unread: bool = True
    is_group: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLM(Protocol):
    def stream_chat(
        self,
        messages: list[ChatMsg],
        tools: list[ToolSchema] | None = None,
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]: ...

    async def complete(
        self,
        messages: list[ChatMsg],
        *,
        role: LLMRole = "chat",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str: ...

    def list_models(self) -> list[str]: ...

    def health(self) -> bool: ...


@runtime_checkable
class STT(Protocol):
    def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> Transcript: ...


@runtime_checkable
class TTS(Protocol):
    def synth(self, text: str) -> AudioChunk: ...


@runtime_checkable
class Connector(Protocol):
    def fetch_since(self, since: datetime) -> list[Item]: ...


@runtime_checkable
class MemoryStore(Protocol):
    def add(self, fact: Fact) -> str: ...

    def search(self, query: str, k: int = 5) -> list[Fact]: ...

    def list(self, kind: str | None = None) -> list[Fact]: ...

    def delete(self, fact_id: str) -> None: ...
