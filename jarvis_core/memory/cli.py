"""``jarvisd forget`` and ``jarvisd memory``."""

from __future__ import annotations

import sys

from jarvis_core.memory.store import SQLiteMemory


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


def list_cli() -> int:
    mem = SQLiteMemory()
    facts = mem.list()
    for f in facts:
        print(f"{f.kind:10} {f.text}")
    if not facts:
        print("(memory is empty)")
    return 0
