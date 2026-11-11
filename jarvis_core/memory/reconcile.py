"""Keep memory tidy: a new fact that says the same thing, or corrects an older one, replaces it instead of piling up.

Without this, "my favourite colour is green" and, a month later, "my favourite colour is blue" both stay, and recall
reads out both. Here the newer statement wins, and the caller is told what it replaced so Jarvis can say so.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jarvis_core.interfaces import Fact
from jarvis_core.memory.store import SQLiteMemory, tokens

MERGE_KINDS = ("fact", "preference", "person", "loop")
EXPLICIT_THRESHOLD = 0.5  # token overlap at which an explicit statement replaces an older one with no one to judge it
JUDGE_THRESHOLD = 0.34  # overlap at which a candidate is worth showing to a judge (the model)

Judge = Callable[[Fact, list[Fact]], Awaitable[str | None]]  # returns the id of the fact to replace, or None to just add


@dataclass
class Outcome:
    id: str
    action: str  # "added" | "same" | "updated"
    replaced: Fact | None = None


def overlap(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def candidates(mem: SQLiteMemory, fact: Fact, threshold: float) -> list[tuple[float, Fact]]:
    """Existing facts of the same family that this one may duplicate or correct, best first."""
    out: list[tuple[float, Fact]] = []
    alias = str(fact.meta.get("alias", "")).lower()
    for old in mem.list():
        if old.id == fact.id or old.kind not in MERGE_KINDS or old.kind != fact.kind:
            continue
        score = overlap(fact.text, old.text)
        if alias and str(old.meta.get("alias", "")).lower() == alias:
            score = max(score, 1.0)  # "my brother is X" always replaces the old "my brother is Y"
        if score >= threshold:
            out.append((score, old))
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()).rstrip(".") == " ".join(b.lower().split()).rstrip(".")


async def apply(mem: SQLiteMemory, fact: Fact, judge: Judge | None = None) -> Outcome:
    """Store ``fact``, replacing the older fact it updates if there is one."""
    if fact.kind in MERGE_KINDS:
        found = candidates(mem, fact, JUDGE_THRESHOLD if judge else EXPLICIT_THRESHOLD)
        for _, old in found:
            if _same(old.text, fact.text):
                return Outcome(mem.add(fact), "same")
        if found:
            target: Fact | None = None
            if judge is not None:
                chosen = await judge(fact, [f for _, f in found[:3]])
                target = next((f for _, f in found if f.id == chosen), None)
            else:
                target = found[0][1]
            if target is not None:
                mem.replace(target.id, fact)
                return Outcome(target.id, "updated", replaced=target)
    return Outcome(mem.add(fact), "added")


JUDGE_SYSTEM = (
    "You keep a person's memory tidy. Given a NEW fact and some EXISTING ones, decide whether NEW replaces one of them "
    "(it corrects it, updates it, or says the same thing in other words). If the facts can both be true (two different "
    "things the person likes, two different people), do not replace. "
    'Reply with only JSON: {"replace": "<id of the existing fact>"} or {"replace": null}.'
)


def llm_judge(llm) -> Judge:
    """A judge that asks the fast model. Any failure means "just add": a duplicate is better than a wrong deletion."""
    import json
    import re

    async def judge(new: Fact, existing: list[Fact]) -> str | None:
        listing = "\n".join(f"{f.id}: {f.text}" for f in existing)
        try:
            raw = await llm.complete(
                [{"role": "system", "content": JUDGE_SYSTEM},
                 {"role": "user", "content": f"NEW: {new.text}\nEXISTING:\n{listing}"}],
                role="fast", temperature=0.0, max_tokens=60)
            m = re.search(r"\{.*\}", raw, re.S)
            chosen = json.loads(m.group(0)).get("replace") if m else None
        except Exception:
            return None
        return chosen if chosen in {f.id for f in existing} else None

    return judge
