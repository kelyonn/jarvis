"""What the Shell's Memory window asks of the daemon: list, search, edit, forget one, forget everything.

Pure functions over :class:`SQLiteMemory`, so they are tested without a window or a socket. Every reply is a plain dict
with ``ok``; a failure carries a short ``error`` the window can show. Text is redacted for display (passwords, keys and
card numbers never appear in the window). Editing a row that was shown redacted replaces the stored wording with what
you typed.
"""

from __future__ import annotations

import time
from typing import Any

from jarvis_core.memory.cli import _edited, _show
from jarvis_core.memory.store import SQLiteMemory

GROUPS = (("person", "People"), ("preference", "Preferences"), ("fact", "Facts"), ("loop", "Loops"))
MAX_TEXT = 300
MAX_ROWS = 500
OPS = ("memory.list", "memory.forget", "memory.edit", "memory.clear")


def _row(f) -> dict[str, Any]:
    return {"id": f.id, "text": _show(f.text),
            "date": time.strftime("%Y-%m-%d", time.localtime(f.created_at)) if f.created_at else ""}


def list_facts(mem: SQLiteMemory, query: str = "") -> dict[str, Any]:
    kinds = tuple(k for k, _ in GROUPS)
    q = (query or "").strip()
    facts = mem.search(q, k=MAX_ROWS, kinds=kinds) if q else [f for f in mem.list() if f.kind in kinds][:MAX_ROWS]
    groups = []
    for kind, title in GROUPS:
        items = [_row(f) for f in facts if f.kind == kind]
        if items:
            groups.append({"kind": kind, "title": title, "items": items})
    return {"ok": True, "groups": groups, "total": sum(len(g["items"]) for g in groups), "query": q}


def forget(mem: SQLiteMemory, fact_id: str) -> dict[str, Any]:
    old = mem.get(str(fact_id or "").strip())
    if old is None:
        return {"ok": False, "error": "That memory is already gone."}
    mem.delete(old.id)
    return {"ok": True, "id": old.id}


def edit(mem: SQLiteMemory, fact_id: str, text: str) -> dict[str, Any]:
    old = mem.get(str(fact_id or "").strip())
    if old is None:
        return {"ok": False, "error": "That memory is gone, so I can't change it."}
    new_text = " ".join(str(text or "").split())
    if not new_text:
        return {"ok": False, "error": "A memory can't be empty. Use delete to remove it."}
    if len(new_text) > MAX_TEXT:
        return {"ok": False, "error": f"That is too long: keep it under {MAX_TEXT} characters."}
    mem.replace(old.id, _edited(old, new_text))
    return {"ok": True, "id": old.id}


def clear(mem: SQLiteMemory) -> dict[str, Any]:
    return {"ok": True, "forgotten": mem.clear()}


def handle(mem: SQLiteMemory, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one window request. Unknown ops and unexpected failures come back as errors, never as exceptions."""
    try:
        if op == "memory.list":
            return list_facts(mem, str(args.get("query") or ""))
        if op == "memory.forget":
            return forget(mem, str(args.get("id") or ""))
        if op == "memory.edit":
            return edit(mem, str(args.get("id") or ""), str(args.get("text") or ""))
        if op == "memory.clear":
            return clear(mem)
    except Exception as e:  # noqa: BLE001 - the window must always get an answer
        return {"ok": False, "error": f"Memory error: {type(e).__name__}"}
    return {"ok": False, "error": f"unknown memory op {op!r}"}
