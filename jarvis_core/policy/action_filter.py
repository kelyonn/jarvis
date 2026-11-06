"""Jarvis never sends, posts, buys or deletes on its own.

Every synthesized keystroke or click that Jarvis wants to make goes through here first. The Swift
shell enforces the same rules independently (defence in depth).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis_core.config import SafetyCfg

TEXT_ROLES = {"axtextfield", "axtextarea", "axcombobox", "axsearchfield", "axwebarea"}
SEND_APPS = (
    "whatsapp", "messages", "mail", "slack", "telegram", "signal", "discord", "teams", "outlook", "linkedin",
    "chrome", "safari", "firefox", "arc", "brave", "edge",
)
RETURN_KEYS = {"return", "enter", "numpadenter", "kp_enter"}
# keys that are always fine
SAFE_KEYS = {"escape", "tab", "up", "down", "left", "right", "delete", "space", "home", "end", "pageup", "pagedown"}
# modifier chords that are fine with a letter key (paste, select all, copy, find, new tab, etc.)
SAFE_CHORD_LETTERS = set("vacxfzltnwr")


@dataclass
class Verdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class ActionFilter:
    def __init__(self, safety: SafetyCfg | None = None) -> None:
        self.safety = safety or SafetyCfg()
        labels = [re.escape(label.lower()) for label in self.safety.never_click_labels]
        self._label_re = re.compile(r"\b(?:" + "|".join(labels) + r")\b") if labels else None

    # -- keys ---------------------------------------------------------------------------------
    def check_key(self, key: str, modifiers: list[str] | None = None, *, app: str = "", focused_role: str = "") -> Verdict:
        k = key.lower().strip()
        mods = {m.lower() for m in (modifiers or [])}
        cmd = bool(mods & {"cmd", "command"})
        if k in RETURN_KEYS:
            if cmd:
                return Verdict(False, "Cmd+Return sends in most apps, so I never press it")
            in_text = focused_role.lower() in TEXT_ROLES
            messaging = any(a in app.lower() for a in SEND_APPS)
            if in_text or messaging or not (app or focused_role):
                return Verdict(False, "I never press Return in a compose field; sending is up to you")
            return Verdict(True)
        if len(k) == 1 and mods:
            if cmd and k in SAFE_CHORD_LETTERS and not (mods & {"ctrl", "control"}):
                return Verdict(True)
            if mods <= {"shift"}:
                return Verdict(True)
            return Verdict(False, f"I don't use the {'+'.join(sorted(mods))}+{k} shortcut")
        if k in SAFE_KEYS or len(k) == 1 or re.fullmatch(r"f\d{1,2}", k):
            return Verdict(True)
        return Verdict(False, f"I don't press '{key}'")

    # -- clicks --------------------------------------------------------------------------------
    def check_click(self, label: str = "") -> Verdict:
        if label and self._label_re and self._label_re.search(label.lower()):
            return Verdict(False, f"I never click '{label}'")
        return Verdict(True)

    def check_type(self, text: str, *, secure: bool = False) -> Verdict:
        if secure:
            return Verdict(False, "I never type into password fields")
        return Verdict(True)
