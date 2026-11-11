"""Assistant facade: one object that turns user text into a streamed reply.

Order of handling for each utterance:
1. a pending spoken confirmation ("shall I ...?") is resolved first,
2. conversation enders ("thanks, that's all"),
3. the fast intent router (regex, no LLM) for short unambiguous commands,
4. the tool-calling agent.
"""

from __future__ import annotations

import random
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from jarvis_core import paths
from jarvis_core.brain.agent import Agent
from jarvis_core.brain.history import ConversationHistory
from jarvis_core.config import Config
from jarvis_core.frontend import Frontend
from jarvis_core.interfaces import LLM
from jarvis_core.router import IntentRouter, normalise
from jarvis_core.skills.base import ToolContext, ToolRegistry
from jarvis_core.skills.registry import build_registry

_ENDERS = re.compile(
    r"^(?:thanks|thank you|thank you very much|cheers|that(?:s| is| will be) (?:all|it|enough)|nothing else|"
    r"no thanks|no thank you|im good|i am good|all good|thats everything|that is everything)"
    r"(?: (?:jarvis|sir|mate|thats all|that will be all|for now))*$"
)
_ENDER_REPLIES = ["Always a pleasure, sir.", "Very good, sir.", "At your service, sir.", "Of course, sir."]


@dataclass
class TurnOutcome:
    follow_up: bool = True  # keep listening for a reply without the wake word
    end_conversation: bool = False
    mute: bool = False  # "go to sleep": ignore the wake word until the hotkey


class Assistant:
    def __init__(
        self,
        cfg: Config,
        llm: LLM,
        history: ConversationHistory,
        *,
        agent: Agent | None = None,
        router: IntentRouter | None = None,
        ctx: ToolContext | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self.cfg = cfg
        self.llm = llm
        self.history = history
        self.agent = agent
        self.router = router
        self.ctx = ctx
        self.registry = registry
        self.last_turn = TurnOutcome()

    @classmethod
    def create(
        cls,
        cfg: Config,
        *,
        llm: LLM,
        text_only: bool = False,
        frontend: Frontend | None = None,
        ctx: ToolContext | None = None,
        registry: ToolRegistry | None = None,
        router: IntentRouter | None = None,
    ) -> Assistant:
        m = cfg.memory
        history = ConversationHistory(
            max_turns=m.conversation_turns, expiry_s=m.conversation_minutes * 60,
            path=paths.conversation_file() if m.enabled and m.persist_conversation else None,
        )
        ctx = ctx or ToolContext(cfg=cfg, frontend=frontend)
        registry = registry or build_registry()
        agent = Agent(llm, cfg, registry, history, ctx)
        a = cls(cfg, llm, history, agent=agent, router=router or IntentRouter.load(), ctx=ctx, registry=registry)
        agent.context_provider = a._prompt_context
        return a

    # ------------------------------------------------------------------ context

    def _prompt_context(self, user_text: str) -> dict[str, Any]:
        ctx = self.ctx
        out: dict[str, Any] = {
            "city": (self.cfg.devices.location_city or (ctx.state.get("city") if ctx else "")) or "unknown",
            "focus": (ctx.state.get("focus") if ctx else None) or "off",
        }
        presence = ctx.services.get("presence") if ctx else None
        if presence is not None:
            try:
                out["focus"] = presence.focus_label()
            except Exception:
                pass
        memory = ctx.services.get("memory") if ctx else None
        if memory is not None:
            try:
                snip = getattr(memory, "snippets", None)
                from jarvis_core.memory.voice import second_person

                out["memory_snippets"] = [second_person(t) for t in (snip(user_text, 5) if snip else [f.text for f in memory.search(user_text, k=5)])]
                recent = getattr(memory, "recent_episodes", None)
                if recent is not None and sum(1 for m in self.history.messages() if m.get("role") == "user") <= 1:
                    # A fresh conversation: remind the model what you last talked about, so you can pick it up.
                    out["recent_talk"] = recent(3)
            except Exception:
                pass
        return out

    # --------------------------------------------------------------------- turn

    @property
    def speech_cap(self) -> int | None:
        """How many sentences of the current reply to voice (the chat card still shows all of it)."""
        plan = getattr(self.agent, "plan", None) if self.agent is not None else None
        return plan.max_sentences if plan is not None else None

    async def respond_stream(self, text: str) -> AsyncIterator[str]:
        self.last_turn = TurnOutcome()
        agent = self.agent
        if agent is None:  # plain chat (tests / degraded mode)
            from jarvis_core.brain.chat import chat_turn

            async for chunk in chat_turn(self.llm, self.cfg, self.history, text):
                yield chunk
            return

        if not agent.awaiting_confirmation:
            if _ENDERS.match(normalise(text)):
                reply = random.choice(_ENDER_REPLIES)
                self.history.add({"role": "user", "content": text})
                self.history.add({"role": "assistant", "content": reply})
                self.last_turn.follow_up = False
                self.last_turn.end_conversation = True
                yield reply
                return
            if self.router is not None:
                routed = self.router.match(text)
                if routed is not None:
                    reply = await agent.try_direct(text, routed)
                    if reply is not None:
                        self.last_turn.follow_up = routed.follow_up
                        yield reply
                        return
        async for chunk in agent.respond(text):
            yield chunk
        if agent.awaiting_confirmation:
            self.last_turn.follow_up = True
