"""Plain (tool-less) chat turn. The tool-calling agent lives in :mod:`brain.agent`."""

from __future__ import annotations

from collections.abc import AsyncIterator

from jarvis_core.brain.history import ConversationHistory
from jarvis_core.brain.persona import build_system_prompt
from jarvis_core.config import Config
from jarvis_core.interfaces import LLM, TextDelta


async def chat_turn(
    llm: LLM, cfg: Config, history: ConversationHistory, user_text: str
) -> AsyncIterator[str]:
    history.add({"role": "user", "content": user_text})
    messages = [{"role": "system", "content": build_system_prompt(cfg)}, *history.messages()]
    parts: list[str] = []
    async for ev in llm.stream_chat(messages, None, role="chat"):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
            yield ev.text
    history.add({"role": "assistant", "content": "".join(parts).strip()})
