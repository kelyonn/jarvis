"""Turns what the user says into durable facts.

Only the USER's own words are ever mined: never tool output, web pages, emails or messages (those are untrusted data
and must not be able to plant memories).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jarvis_core.interfaces import Fact
from jarvis_core.router import normalise

log = logging.getLogger(__name__)

CONFIDENCE_MIN = 0.7
_CUES = re.compile(r"\b(?:my|i am|im|i like|i love|i hate|i prefer|i usually|i always|i never|i work|i live|call me|remember)\b", re.I)
_PERSON = re.compile(
    r"^(?:that )?my (?P<alias>[a-z][a-z ]{1,28}?)(?:['’]s name)? (?:is|are|called|named) (?P<name>[A-Za-z][A-Za-z.'\- ]{1,40})$", re.I)
_RELATIONS = frozenset(
    "brother sister mom mum mother dad father wife husband partner girlfriend boyfriend son daughter uncle aunt cousin "
    "friend boss manager colleague roommate flatmate grandma grandmother grandpa grandfather".split()
)

EXTRACT_SYSTEM = (
    "Extract durable facts about the USER from their message: preferences, people in their life, habits, "
    "places, commitments. Ignore questions, commands, small talk and anything temporary. "
    'Reply with only a JSON list of {"kind": "fact|preference|person|loop", "text": "<third person, <=20 words>", '
    '"confidence": 0-1}. Reply [] if nothing is worth remembering.'
)


def parse_statement(text: str) -> Fact:
    """Build a Fact from an explicit 'remember ...' statement. 'my brother is Arjun' becomes a contact alias."""
    t = text.strip().rstrip(".!")
    t = re.sub(r"^(?:that|to remember that)\s+", "", t, flags=re.I)
    m = _PERSON.match(t)
    if m:
        alias = re.sub(r"\s+", " ", m["alias"].lower().strip())
        if alias in _RELATIONS or alias.split()[-1] in _RELATIONS:
            name = m["name"].strip().title()
            return Fact(kind="person", text=f"The user's {alias} is {name}", meta={"alias": alias, "name": name})
    low = t.lower()
    kind = "preference" if re.match(r"^(?:i (?:prefer|like|love|hate|always|usually|never)|my favou?rite)", low) else "fact"
    if re.match(r"^(?:remind me|i need to follow up|follow up)", low):
        kind = "loop"
    t = re.sub(r"^i\b", "I", t)
    return Fact(kind=kind, text=t[:300])


def _parse_list(raw: str) -> list[dict[str, Any]]:
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


class Extractor:
    """Hook for ``Hooks.on_turn_done``: quietly remembers lasting facts the user mentions."""

    def __init__(self, memory, llm, enabled: bool = True) -> None:
        self.memory, self.llm, self.enabled = memory, llm, enabled
        self.last_ids: list[str] = []

    def worth_a_look(self, text: str) -> bool:
        t = normalise(text)
        return len(t.split()) >= 4 and bool(_CUES.search(text))

    async def on_turn_done(self, user_text: str, reply: str) -> None:
        if not self.enabled or self.llm is None or not self.worth_a_look(user_text):
            return
        if re.match(r"^\s*(?:forget|remember)\b", user_text, re.I):
            return  # handled explicitly
        try:
            raw = await self.llm.complete(
                [{"role": "system", "content": EXTRACT_SYSTEM}, {"role": "user", "content": user_text[:600]}],
                role="fast", temperature=0.0, max_tokens=250)
        except Exception:
            log.debug("memory extraction skipped", exc_info=True)
            return
        for d in _parse_list(raw)[:3]:
            try:
                conf = float(d.get("confidence", 0))
                kind = d.get("kind") if d.get("kind") in ("fact", "preference", "person", "loop") else "fact"
                text = str(d.get("text", "")).strip()
            except (TypeError, ValueError):
                continue
            if conf < CONFIDENCE_MIN or len(text) < 6:
                continue
            fact = Fact(kind=kind, text=text[:300], confidence=conf)
            if kind == "person":
                p = parse_statement(text.replace("The user's", "my"))
                if p.kind == "person":
                    fact = p
            try:
                self.last_ids.append(self.memory.add(fact))
            except ValueError:
                continue


class EpisodeRecorder:
    """Hook for ``Hooks.on_turn_done``: a one-line dated note of each exchange, so "what did we talk about yesterday" and
    "going back to that thing about X" work. Only the user's words and Jarvis's own reply are used."""

    def __init__(self, memory, enabled: bool = True, clock=None) -> None:
        import time

        self.memory, self.enabled, self._clock = memory, enabled, clock or time.time

    async def on_turn_done(self, user_text: str, reply: str) -> None:
        from datetime import datetime

        from jarvis_core.logging_setup import redact

        user, said = redact(user_text).strip(), redact(reply).strip()
        if not self.enabled or len(user.split()) < 3 or not said:
            return
        now = self._clock()
        when = datetime.fromtimestamp(now).strftime("%a %d %b, %H:%M")
        text = f"[{when}] You said: {user[:160]} | I replied: {said[:200]}"
        try:
            self.memory.add(Fact(kind="episode", text=text, created_at=now, confidence=0.5))
        except ValueError:
            pass
