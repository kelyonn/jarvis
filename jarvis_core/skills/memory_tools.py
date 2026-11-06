"""Voice control over memory: remember, recall, forget."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.interfaces import Fact
from jarvis_core.memory.extractor import parse_statement
from jarvis_core.skills.base import Decision, S, Tool, ToolContext, ToolRegistry, obj

LAST_KEY = "last_fact_id"


def _mem(ctx: ToolContext):
    m = ctx.services.get("memory")
    if m is None:
        raise SkillError("My memory isn't available right now, sir.")
    return m


def _plain(f: Fact) -> str:
    return f.text.rstrip(". ")


async def _remember(ctx: ToolContext, a: dict[str, Any]) -> str:
    mem = _mem(ctx)
    text = (a.get("text") or "").strip()
    if not text:
        raise SkillError("What shall I remember, sir?")
    fact = parse_statement(text)
    ctx.state[LAST_KEY] = mem.add(fact)
    ctx.audit.write("memory_add", fact_kind=fact.kind, chars=len(fact.text))
    if fact.kind == "person":
        return f"Noted, sir. {fact.meta['name']} is your {fact.meta['alias']}."
    return "Noted, sir."


async def _recall(ctx: ToolContext, a: dict[str, Any]) -> str:
    mem = _mem(ctx)
    q = (a.get("query") or "").strip()
    if q:
        facts = mem.about(q) or mem.search(q, k=5)
        if not facts:
            return f"I don't have anything about {q}, sir."
    else:
        facts = [f for f in mem.list() if f.kind not in ("style", "episode")]
        if not facts:
            return "I don't know much about you yet, sir. Tell me to remember things and I will."
    shown = facts[:6]
    more = len(facts) - len(shown)
    text = "; ".join(_plain(f) for f in shown)
    return f"{text}." + (f" And {more} more, sir." if more > 0 else "")


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
        "forget",
        "Delete memories. No query forgets the most recently stored item ('forget that'); a query forgets everything about that person/topic.",
        obj({"query": S("Optional person or topic to forget everything about")}), _forget,
        classify=_classify_forget, category="memory"))
