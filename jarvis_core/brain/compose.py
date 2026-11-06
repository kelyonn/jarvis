"""Turns raw tool output (unread messages, calendar, mail, files) into something a person would actually say."""

from __future__ import annotations

import logging
import re

from jarvis_core.brain.persona import wrap_untrusted
from jarvis_core.config import Config
from jarvis_core.interfaces import LLM

log = logging.getLogger(__name__)

# Tools whose output is information about the user's life rather than a confirmation: these get a considered reply.
COMPOSE_TOOLS = frozenset({
    "read_messages", "check_linkedin", "search_messages", "read_conversation",
    "gcal_agenda", "gcal_search", "gdrive_search", "gdrive_read", "get_calendar",
})

_PROMPT = """\
You are {assistant}, {name}'s assistant, replying out loud to what they just asked. Below is the raw data you fetched.
Answer their question the way a sharp friend would: think about what they actually want, lead with what matters, and keep
it to a few spoken sentences.

Rules:
- Say what people WANT or SAID in plain words, not how many messages or what time. Skip counts and clock times unless they matter.
  Never list "N messages from X". Turn it into meaning: who needs a reply, who is just chatting, what is time-sensitive.
- Most important first: anything with a deadline, a question aimed at them, or a person they care about.
- Cover the top two or three people at most in a few sentences, then offer the rest ("want me to go through the others?").
- If the data is empty or says it could not be read, say that plainly and what would fix it. Never invent anything.
- Contractions, relaxed tone, no "sir", no stock openers like "You've got", no markdown, lists, emoji or URLs.

Example of the tone wanted, for unread messages:
"Naan's waiting on the notes before tonight, and Deepika wants to know if dinner Friday is still on. Your mom's also been
trying to reach you. Dhanyatha just sent over an assignment PDF, nothing urgent. Want me to read any of them out?"

What they asked: {ask}
"""


async def compose_reply(llm: LLM, cfg: Config, ask: str, data: str, *, assistant: str = "Jarvis") -> str | None:
    """A considered spoken answer built from ``data``; ``None`` if the model could not be reached (caller falls back)."""
    if not data.strip():
        return None
    system = _PROMPT.format(assistant=assistant, name=cfg.user.name, ask=ask.strip() or "(unspecified)")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": wrap_untrusted(data[:6000], "tool")}]
    try:
        out = await llm.complete(messages, role="chat", temperature=0.5, max_tokens=260)
    except Exception as e:  # network, auth, rate limit: the plain summary still works
        log.warning("compose failed (%s); using the plain summary", e)
        return None
    out = re.sub(r"</?untrusted[^>]*>", "", out or "").strip()
    return out or None
