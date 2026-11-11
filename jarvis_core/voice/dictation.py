"""Hold a key, speak, let go: the words are typed into whatever app is in front.

This is dictation, not a request: nothing is sent to the assistant and nothing is spoken back. The Shell types the text
(it never presses Return and refuses password fields), and apps set to ``never`` in ``safety.app_rules`` are skipped.
"""

from __future__ import annotations

import re

_FILLERS = re.compile(r"\b(?:um+|uh+|uhh+|erm+|er|ah+|hmm+|mm+)\b[,.]?\s*", re.I)
_STUTTER = re.compile(r"\b(\w+)(?:\s+\1\b)+", re.I)
_PUNCT = (
    (re.compile(r"\s*\b(?:question mark)\b", re.I), "?"),
    (re.compile(r"\s*\b(?:exclamation (?:mark|point))\b", re.I), "!"),
    (re.compile(r"\s*\b(?:full stop|period)\b(?=\s|$)", re.I), "."),
    (re.compile(r"\s*\bcomma\b", re.I), ","),
    (re.compile(r"\s*\bsemicolon\b", re.I), ";"),
    (re.compile(r"\s*\bcolon\b", re.I), ":"),
)
_NEWLINE = re.compile(r"\s*\b(?:new line|new paragraph|next line)\b", re.I)


def clean_dictation(text: str, *, spoken_punctuation: bool = True) -> str:
    """Tidy what was said: drop "um" and "uh", collapse a stuttered word, turn "comma" and "question mark" into
    punctuation, capitalise the start. Returns single-line text, because the Shell never types a Return."""
    t = " ".join((text or "").split())
    if not t:
        return ""
    t = _FILLERS.sub("", t)
    t = _STUTTER.sub(lambda m: m.group(1), t)
    t = _NEWLINE.sub(" ", t)  # a line break would need Return, which is never typed: a space keeps the words apart
    if spoken_punctuation:
        for rx, mark in _PUNCT:
            t = rx.sub(mark, t)
        t = re.sub(r"([,.;:?!])(?=[A-Za-z])", r"\1 ", t)  # a space after punctuation that was glued to the next word
    t = re.sub(r"\s+([,.;:?!])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    t = re.sub(r"([.!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)  # a new sentence starts with a capital
    return t[:1].upper() + t[1:] if t else ""
