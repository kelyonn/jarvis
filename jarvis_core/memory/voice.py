"""Facts are stored in the person's own words ("I'm 20", "My name is Kalyan"), so spoken back to them they must become "you".

Without this Jarvis reads "My name is Kalyan, I was born in ..." as if it were describing itself."""

from __future__ import annotations

import re

_SWAPS = [
    (r"\bI['’]m\b", "you're"), (r"\bI am\b", "you are"), (r"\bI was\b", "you were"),
    (r"\bI['’]ve\b", "you've"), (r"\bI['’]ll\b", "you'll"), (r"\bI['’]d\b", "you'd"),
    (r"\bmyself\b", "yourself"), (r"\bmine\b", "yours"), (r"\bmy\b", "your"), (r"\bme\b", "you"), (r"\bI\b", "you"),
]
_SWAPS = [(re.compile(p, re.I), r) for p, r in _SWAPS]
_IRREGULAR = {"has": "have", "is": "are", "was": "were", "does": "do", "goes": "go", "doesn't": "don't", "isn't": "aren't",
              "hasn't": "haven't", "wasn't": "weren't"}
_USER = re.compile(r"^(?:the )?user(?:['’]s)? ", re.I)


def _verb(word: str) -> str:
    w = word.lower()
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ches", "shes", "sses", "xes")):
        return w[:-2]
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def second_person(text: str) -> str:
    """"My name is Kal, I'm 20" -> "Your name is Kal, you're 20". Text already about "you" is left alone."""
    t = text.strip()
    if not t:
        return t
    m = _USER.match(t)
    if m:  # "User prefers tea" / "The user's brother is Arjun"
        rest = t[m.end():]
        if m.group(0).lower().rstrip().endswith(("'s", "’s")):
            t = "Your " + rest
        else:
            first, _, tail = rest.partition(" ")
            t = f"You {_verb(first)} {tail}".strip()
    for pat, rep in _SWAPS:
        t = pat.sub(rep, t)
    # "I" -> "you" in the middle of a sentence stays lower case; a sentence start gets its capital back
    t = re.sub(r"(^|[.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    return t
