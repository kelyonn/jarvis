"""``jarvisd forget`` and ``jarvisd memory``: see, search, correct and delete what Jarvis remembers."""

from __future__ import annotations

import json
import sys
import time

from jarvis_core.interfaces import Fact
from jarvis_core.logging_setup import redact as redact_keys
from jarvis_core.memory.store import SQLiteMemory
from jarvis_core.policy.redact import redact as redact_secrets

MIN_PREFIX = 4
GROUPS = (("person", "People"), ("preference", "Preferences"), ("fact", "Facts"), ("loop", "Loops"))


def forget_cli(all_: bool = False, person: str | None = None) -> int:
    mem = SQLiteMemory()
    if all_:
        print(f"forgot {mem.clear()} memories")
        return 0
    if person:
        print(f"forgot {mem.delete_many(mem.about(person))} memories about {person}")
        return 0
    print("use --all or --person NAME", file=sys.stderr)
    return 2


def _show(text: str) -> str:
    """Text for the screen: never print a password or key that ended up in memory."""
    return redact_secrets(redact_keys(" ".join(text.split())))[0]


def _row(f: Fact) -> dict:
    return {"id": f.id, "kind": f.kind, "date": time.strftime("%Y-%m-%d", time.localtime(f.created_at)) if f.created_at else "",
            "text": _show(f.text)}


def _resolve(mem: SQLiteMemory, ident: str) -> Fact:
    """A fact by full id, or by an unambiguous prefix of at least four characters. Raises LookupError with a plain message."""
    ident = ident.strip()
    exact = mem.get(ident) if ident else None
    if exact is not None:
        return exact
    if len(ident) < MIN_PREFIX:
        raise LookupError(f"no memory with id {ident!r} (a prefix needs at least {MIN_PREFIX} characters)")
    found = [f for f in mem.list() if f.id.startswith(ident)]
    if not found:
        raise LookupError(f"no memory with id {ident!r}")
    if len(found) > 1:
        raise LookupError(f"{ident!r} matches {len(found)} memories; use more of the id")
    return found[0]


def _edited(old: Fact, text: str) -> Fact:
    from jarvis_core.memory.extractor import parse_statement

    meta = dict(old.meta)
    kind = old.kind
    if old.kind == "person":
        parsed = parse_statement(text.replace("The user's", "my"))
        if parsed.kind == "person":
            return Fact(kind="person", text=parsed.text, meta=parsed.meta, created_at=time.time(), confidence=old.confidence)
    return Fact(kind=kind, text=text.strip(), meta=meta, created_at=time.time(), confidence=old.confidence)


def list_cli(
    *,
    search: str | None = None,
    forget: str | None = None,
    edit: tuple[str, str] | None = None,
    episodes: bool = False,
    styles: bool = False,
    as_json: bool = False,
) -> int:
    mem = SQLiteMemory()
    try:
        if forget is not None:
            f = _resolve(mem, forget)
            mem.delete(f.id)
            print(f"forgot {f.id}: {_show(f.text)}")
            return 0
        if edit is not None:
            old = _resolve(mem, edit[0])
            if not edit[1].strip():
                raise LookupError("the new text is empty")
            mem.replace(old.id, _edited(old, edit[1]))
            new = mem.get(old.id)
            print(f"updated {old.id}: {_show(new.text)}")
            return 0
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 1

    if search:
        facts = [f for f in mem.search(search, k=20, kinds=None) if f.kind != "style" or styles]
        if not episodes:
            facts = [f for f in facts if f.kind != "episode"]
    else:
        facts = mem.list()
    kinds = [k for k, _ in GROUPS] + (["episode"] if episodes else []) + (["style"] if styles else [])
    facts = [f for f in facts if f.kind in kinds]

    if as_json:
        print(json.dumps([_row(f) for f in facts], indent=2))
        return 0
    if not facts:
        print("(nothing found)" if search else "(memory is empty)")
        return 0
    labels = dict(GROUPS) | {"episode": "Conversations", "style": "Writing styles"}
    for kind in kinds:
        group = sorted((f for f in facts if f.kind == kind), key=lambda f: f.created_at, reverse=True)
        if not group:
            continue
        print(f"{labels[kind]} ({len(group)})")
        for f in group:
            r = _row(f)
            print(f"  {r['id']}  {r['date']}  {r['text']}")
        print()
    return 0
