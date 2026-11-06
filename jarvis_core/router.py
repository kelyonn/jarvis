"""Fast intent router: regex-matches short commands so they skip the LLM entirely."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_FILLER_PREFIX = re.compile(
    r"^(?:(?:hey|ok|okay|hi|yo)\s+)?(?:(?:jarvis|jarvis)[, ]+)?"
    r"(?:(?:please|kindly)\s+)?"
    r"(?:(?:can|could|would|will) you(?: please)?\s+|i (?:want|need|would like) you to\s+|(?:id|i would) like (?:you )?to\s+|(?:lets|let us)\s+)?"
    r"(?:(?:please|kindly)\s+)?",
)
_FILLER_SUFFIX = re.compile(r"(?:\s+(?:please|for me|sir|jarvis|jarvis|now|thanks|thank you|right now|would you))+$")

_WORD_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty five": 45, "fifty": 50, "sixty": 60,
}
_NUM_WORDS_RE = re.compile(
    r"\b(" + "|".join(sorted((w for w in _WORD_NUM if w not in {"a", "an"}), key=len, reverse=True)) + r")\b"
)


def normalise(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"['’‘`]", "", t)  # what's -> whats
    t = re.sub(r"[,;:]", " ", t)
    t = re.sub(r"[.!?]+(?=\s|$)", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    for _ in range(2):
        t = _FILLER_PREFIX.sub("", t).strip()
    t = _FILLER_SUFFIX.sub("", t).strip()
    return t


def to_number(token: str) -> float | None:
    token = token.strip().lower()
    if token in _WORD_NUM:
        return float(_WORD_NUM[token])
    try:
        return float(token)
    except ValueError:
        return None


@dataclass
class Intent:
    name: str
    patterns: list[re.Pattern[str]]
    tool: str
    args: dict[str, str] = field(default_factory=dict)
    reply: str = "Done, sir."
    scale: float = 1.0
    follow_up: bool = True


@dataclass
class RoutedCall:
    intent: str
    tool: str
    args: dict[str, Any]
    reply_template: str
    groups: dict[str, str]
    follow_up: bool = True

    def render_reply(self, result: str) -> str:
        data = {**self.groups, **{k: v for k, v in self.args.items()}, "result": result}
        try:
            return vary(self.reply_template).format_map(_Safe(data))
        except Exception:
            return result or "Done, sir."


# Canned acknowledgements get alternates so the same command never sounds like the same recording twice.
VARIANTS: dict[str, list[str]] = {
    "Opening {name}, sir.": ["Opening {name}, sir.", "Right, {name} coming up.", "On it.", "{name}, one moment."],
    "{name} is up, sir.": ["{name} is up, sir.", "There you go, {name}.", "Switched to {name}."],
    "Closing {name}, sir.": ["Closing {name}, sir.", "Closing {name} now.", "Right, {name} is closing."],
    "Louder, sir.": ["Louder, sir.", "Turning it up.", "A bit more volume."],
    "Quieter, sir.": ["Quieter, sir.", "Turning it down.", "A little softer."],
    "Muted, sir.": ["Muted, sir.", "Silence it is.", "Muted."],
    "Sound is back, sir.": ["Sound is back, sir.", "And we're back.", "Unmuted."],
    "Brighter, sir.": ["Brighter, sir.", "Brightening up.", "A bit brighter."],
    "Dimmer, sir.": ["Dimmer, sir.", "Dimming it.", "Easing the brightness down."],
    "Right away, sir.": ["Right away, sir.", "On it.", "Of course.", "Done."],
    "Skipping, sir.": ["Skipping, sir.", "Next one.", "Skipping ahead."],
    "Going back, sir.": ["Going back, sir.", "Back we go.", "Previous track."],
    "Locking, sir.": ["Locking, sir.", "Locking up.", "Locked."],
    "Dark mode on, sir.": ["Dark mode on, sir.", "Dark mode, easier on the eyes.", "Going dark."],
    "Light mode, sir.": ["Light mode, sir.", "Back to light mode.", "Lights on."],
    "Timer set, sir.": ["Timer set, sir.", "Timer's running.", "You're on the clock."],
    "Timer cancelled, sir.": ["Timer cancelled, sir.", "Timer's gone.", "Cancelled."],
    "Done, sir.": ["Done, sir.", "Sorted.", "All done.", "Done."],
    "Bluetooth is on, sir.": ["Bluetooth is on, sir.", "Bluetooth's on.", "Bluetooth is back on."],
    "Bluetooth is off, sir.": ["Bluetooth is off, sir.", "Bluetooth's off.", "Switched Bluetooth off."],
}
_last_pick: dict[str, str] = {}


def vary(template: str, rng: random.Random | None = None) -> str:
    options = VARIANTS.get(template)
    if not options:
        return template
    rng = rng or random
    choices = [o for o in options if o != _last_pick.get(template)] or options
    pick = rng.choice(choices)
    _last_pick[template] = pick
    return pick


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return ""


class IntentRouter:
    def __init__(self, intents: list[Intent]) -> None:
        self.intents = intents

    @classmethod
    def load(cls, path: Path | None = None) -> IntentRouter:
        path = path or Path(__file__).with_name("intents.yaml")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        intents = []
        for item in raw:
            intents.append(
                Intent(
                    name=item["name"],
                    patterns=[re.compile(p) for p in item["patterns"]],
                    tool=item["tool"],
                    args={k: str(v) for k, v in (item.get("args") or {}).items()},
                    reply=item.get("reply", "Done, sir."),
                    scale=float(item.get("scale", 1)),
                    follow_up=bool(item.get("follow_up", True)),
                )
            )
        return cls(intents)

    def match(self, text: str) -> RoutedCall | None:
        t = normalise(text)
        if not t or len(t) > 90:
            return None
        compound = re.match(r"^(?:open|launch|start) (?:spotify|music|apple music) (?:and |then )(?:play|put on) ", t)
        if not compound and re.search(r"\b(?:and|then|also|after that)\b", t):
            return None  # multi-step requests belong to the agent
        for it in self.intents:
            for pat in it.patterns:
                m = pat.match(t)
                if not m:
                    continue
                groups = {k: (v or "").strip() for k, v in m.groupdict().items()}
                if "n" in groups:
                    n = to_number(groups["n"])
                    if n is None:
                        continue
                    n *= it.scale
                    groups["n"] = str(int(n)) if n == int(n) else str(n)
                args = {k: v.format_map(_Safe(groups)) for k, v in it.args.items()}
                args = {k: v for k, v in args.items() if v != ""}
                if it.name == "routine" and "name" not in args:
                    args["name"] = t.replace(" mode", "")
                return RoutedCall(it.name, it.tool, args, it.reply, groups, it.follow_up)
        return None
