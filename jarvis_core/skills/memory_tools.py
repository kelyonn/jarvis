"""Voice control over memory: remember, recall, forget."""

from __future__ import annotations

import re
from typing import Any

from jarvis_core import cards
from jarvis_core.errors import SkillError
from jarvis_core.interfaces import Fact
from jarvis_core.memory.store import FACT_KINDS
from jarvis_core.memory.voice import second_person
from jarvis_core.memory import reconcile
from jarvis_core.memory.extractor import looks_like_action_report, parse_statement
from jarvis_core.skills.base import Decision, I, S, Tool, ToolContext, ToolRegistry, obj

LAST_KEY = "last_fact_id"


def _mem(ctx: ToolContext):
    m = ctx.services.get("memory")
    if m is None:
        raise SkillError("My memory isn't available right now, sir.")
    return m


def _plain(f: Fact) -> str:
    """A fact as it is said back to the person: stored in their own words ("I'm 20"), spoken as "you're 20"."""
    return second_person(f.text.rstrip(". "))


async def _remember(ctx: ToolContext, a: dict[str, Any]) -> str:
    mem = _mem(ctx)
    text = (a.get("text") or "").strip()
    if not text:
        raise SkillError("What shall I remember, sir?")
    if looks_like_action_report(text):
        raise SkillError("That sounds like a task, not a fact about you. Use the notes or reminders tools for it.")
    fact = parse_statement(text)
    outcome = await reconcile.apply(mem, fact)
    ctx.state[LAST_KEY] = outcome.id
    ctx.audit.write("memory_add", fact_kind=fact.kind, chars=len(fact.text), outcome=outcome.action)
    if outcome.action == "updated" and outcome.replaced is not None:
        return f"Noted, sir. That replaces what I had: {_plain(outcome.replaced)}."
    if fact.kind == "person":
        return f"Noted, sir. {fact.meta['name']} is your {fact.meta['alias']}."
    return "Noted, sir."


_IDENTITY = re.compile(
    r"\b(?:my name|i go by|i'm an? |i am an? |i live|i'm in|i'm \d|born|my college|student|batch|cgpa|my github|my linkedin|"
    r"my portfolio|i use a|i work|my job|i train|i prefer|my sem)\b|\bPES\b", re.I)


_CORE = re.compile(r"\b(?:my name is|i go by|i live in|i'm an? [\w ]*student|i'm \d+ as of|i was born|i'm in sem|my cgpa|i use a mac)\b", re.I)


def _most_about_them_first(facts: list[Fact]) -> list[Fact]:
    """For "tell me everything": who they are (name, college, city, age) before the odd preference or the latest detail."""
    def rank(f: Fact) -> tuple[int, float]:
        if _CORE.search(f.text):
            return (0, f.created_at)  # the basics, in the order they were told
        return (1 if _IDENTITY.search(f.text) else 2 if f.kind in ("fact", "person") else 3, -f.created_at)
    return sorted((f for f in facts if f.text.strip()), key=rank)


_TIMELINE = re.compile(r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]* \d{4}:", re.I)


def _overview(facts: list[Fact]) -> list[Fact]:
    """The few facts that say who they are: basics first, no dated timeline notes, no near-duplicates."""
    chosen: list[Fact] = []
    seen: list[set[str]] = []
    for f in _most_about_them_first(facts):
        if _TIMELINE.match(f.text.strip()):
            continue
        bag = set(re.findall(r"[a-z0-9]+", f.text.lower()))
        if any(len(bag & other) / max(1, len(bag | other)) > 0.45 for other in seen):
            continue
        chosen.append(f)
        seen.append(bag)
    return chosen


async def _recall(ctx: ToolContext, a: dict[str, Any]) -> str:
    mem = _mem(ctx)
    q = (a.get("query") or "").strip()
    if q:
        facts = [f for f in (mem.about(q) or mem.search(q, k=8, kinds=FACT_KINDS)) if f.kind in FACT_KINDS]  # never chat logs
        if not facts:
            return f"I don't have anything about {q}, sir."
    else:
        facts = [f for f in mem.list() if f.kind not in ("style", "episode")]
        if not facts:
            return "I don't know much about you yet, sir. Tell me to remember things and I will."
    if not q:
        facts = _overview(facts)
    shown = facts[:7 if not q else 6]
    more = len(facts) - len(shown)
    if not q:
        # Spoken as a few plain sentences about them: not every fact, and never a list of "My ..." read aloud.
        text = ". ".join(_plain(f) for f in shown) + ". There's plenty more if you ask about something specific, sir."
    else:
        text = "; ".join(_plain(f) for f in shown) + "." + (f" And {more} more, sir." if more > 0 else "")
    cards.post(ctx, {"kind": "list", "icon": "brain", "kicker": "What I remember", "items": [{"title": _plain(f)} for f in shown],
                     "footnote": f"And {more} more" if more > 0 else "", "actions": [cards.say("Open memory", "open my memory window")]})
    return text


async def _search_conversations(ctx: ToolContext, a: dict[str, Any]) -> str:
    """Earlier exchanges, newest first: "what did we talk about yesterday", "what did I ask about the flight last week"."""
    mem = _mem(ctx)
    days = max(1, min(int(a.get("days") or 30), 365))
    since = ctx.clock().timestamp() - days * 86400
    q = (a.get("query") or "").strip()
    if q:
        found = mem.search(q, k=5, kinds=("episode",), since=since)
    else:
        found = [f for f in mem.list("episode") if f.created_at >= since][:5]
    if not found:
        return "I don't have a record of that, sir." if q else "We haven't spoken in that time, sir."
    return "\n".join(f.text for f in found)


def _forget_target(ctx: ToolContext, a: dict[str, Any]) -> list[Fact]:
    mem = _mem(ctx)
    q = (a.get("query") or "").strip()
    if q:
        return mem.about(q) or mem.search(q, k=3)
    fid = ctx.state.get(LAST_KEY)
    last = (mem.get(fid) if fid else None) or mem.last()
    return [last] if last else []


def _classify_forget(ctx: ToolContext, a: dict[str, Any]) -> Decision:
    if not (a.get("query") or "").strip():
        return Decision.allow()  # "forget that": a single, just-stated item
    try:
        n = len(_forget_target(ctx, a))
    except SkillError:
        return Decision.allow()
    if n <= 1:
        return Decision.allow()
    return Decision.confirm(f"That will remove {n} things about {a['query'].strip()}, sir. Shall I?")


async def _forget(ctx: ToolContext, a: dict[str, Any]) -> str:
    mem = _mem(ctx)
    targets = _forget_target(ctx, a)
    if not targets:
        return "There's nothing to forget, sir."
    n = mem.delete_many(targets)
    ctx.state.pop(LAST_KEY, None)
    ctx.audit.write("memory_forget", count=n)
    if n == 1:
        return f"Forgotten, sir: {_plain(targets[0])}."
    return f"Forgotten, sir. That was {n} items."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "remember",
        "Store a lasting fact or preference the user asked you to remember, e.g. 'my brother is Arjun' or 'I prefer tea'.",
        obj({"text": S("The thing to remember, in the user's words")}, required=["text"]), _remember, category="memory"))
    reg.register(Tool(
        "recall",
        "What you know about the user, or about a person/topic when query is given ('what do you know about me').",
        obj({"query": S("Optional person or topic")}), _recall, category="memory"))
    reg.register(Tool(
        "search_conversations",
        "Look back through earlier conversations with the user, optionally for a topic and within the last N days "
        "('what did we talk about yesterday', 'what did I ask about the flight last week').",
        obj({"query": S("Optional topic to look for"), "days": I("How many days back, default 30")}),
        _search_conversations, category="memory", untrusted_output=False))
    reg.register(Tool(
        "forget",
        "Delete memories. No query forgets the most recently stored item ('forget that'); a query forgets everything about that person/topic.",
        obj({"query": S("Optional person or topic to forget everything about")}), _forget,
        classify=_classify_forget, category="memory"))
