"""Helpers for walking the accessibility tree JSON returned by the shell's ``ax.tree``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

Node = dict[str, Any]

TEXT_INPUT_ROLES = {"AXTextArea", "AXTextField", "AXComboBox", "AXSearchField", "AXWebArea"}


def walk(node: Node | None) -> Iterator[Node]:
    if not node:
        return
    yield node
    for child in node.get("children") or []:
        yield from walk(child)


def label(node: Node) -> str:
    return " ".join(str(node.get(k, "")) for k in ("title", "desc", "value") if node.get(k)).strip()


def find(tree: Node | None, pred: Callable[[Node], bool]) -> list[Node]:
    return [n for n in walk(tree) if pred(n)]


def find_compose(tree: Node | None, hints: tuple[str, ...] = ("type a message", "write a message", "message", "compose")) -> Node | None:
    """The message compose field: an editable text area whose label looks like a compose box."""
    fields = [n for n in walk(tree) if n.get("role") in {"AXTextArea", "AXTextField"}]
    for hint in hints:
        for n in fields:
            if hint in label(n).lower():
                return n
    return fields[-1] if fields else None


def find_row(tree: Node | None, name: str) -> Node | None:
    """A list row / link / cell whose text mentions ``name`` (case-insensitive) and can be pressed."""
    want = name.lower()
    best: Node | None = None
    for n in walk(tree):
        if n.get("role") not in {"AXRow", "AXCell", "AXLink", "AXButton", "AXStaticText", "AXGroup", "AXOutlineRow"}:
            continue
        text = label(n).lower()
        if want in text:
            if "Press" in (n.get("actions") or []):
                return n
            best = best or n
    return best


def secure_field_present(tree: Node | None) -> bool:
    return any(n.get("role") == "AXSecureTextField" for n in walk(tree))
