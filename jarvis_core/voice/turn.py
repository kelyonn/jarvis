"""Phrase helpers for turn-taking: wake-phrase stripping, stop/yes/no detection."""

from __future__ import annotations

import re

_WAKE_PREFIX = re.compile(
    r"^\s*(?:(?:hey|hi|hello|okay|ok|yo)[\s,]+)?(?:jarvis|jarvas|service|jervis|jarvi)\b[\s,.:;!-]*",
    re.I,
)

_STOP = {
    "stop", "cancel", "never mind", "nevermind", "stop it", "stop that", "be quiet", "quiet",
    "shut up", "enough", "that's enough", "thats enough", "abort", "forget it", "stop talking",
    "halt",
}
_SLEEP = {
    "go to sleep", "sleep", "sleep mode", "sleep now", "mute", "mute yourself", "stop listening",
    "go quiet", "go silent", "go offline", "that's all", "thats all", "goodnight", "good night",
}
_FILLER_EDGES = re.compile(r"^(?:please\s+)+|(?:\s+(?:please|now|jarvis|for now))+$")

_YES = {
    "yes", "yeah", "yep", "yup", "sure", "affirmative", "proceed", "confirm", "confirmed",
    "go ahead", "do it", "please do", "go for it", "absolutely", "correct", "right", "ok", "okay",
    "alright", "fine", "sounds good", "that's right", "thats right", "yes please", "send it",
}
_NO = {
    "no", "nope", "nah", "negative", "don't", "dont", "do not", "cancel", "stop", "abort",
    "never mind", "nevermind", "not now", "no thanks", "no thank you", "wait", "hold on",
}


def _norm(text: str) -> str:
    return re.sub(r"[^\w\s']", "", text.lower()).strip()


def strip_wake_phrase(text: str) -> str:
    end = _wake_phrase_end(text)
    if end is not None:  # drop the lead-in and the wake phrase itself, keep the request
        return text[end:].lstrip(" ,.;:!-").strip()
    out = _WAKE_PREFIX.sub("", text, count=1).strip()
    return out


def is_stop_phrase(text: str) -> bool:
    return _norm(strip_wake_phrase(text) or text) in _STOP


def is_sleep_phrase(text: str) -> bool:
    """"Stop listening", "go to sleep", "jarvis stop listening", "stop listening please" ..."""
    n = _norm(strip_wake_phrase(text) or text)
    return n in _SLEEP or _FILLER_EDGES.sub("", n) in _SLEEP


def is_thanks(text: str) -> bool:
    return _norm(text) in {
        "thanks", "thank you", "thanks jarvis", "thank you jarvis", "cheers", "much appreciated",
        "thanks a lot", "thank you very much", "ta",
    }


def parse_confirmation(text: str | None) -> bool | None:
    """True = yes, False = no, None = unclear/silence."""
    if not text:
        return None
    n = _norm(text)
    if not n:
        return None
    words = set(n.split())
    if n in _NO or any(p in n for p in ("not now", "don't", "dont", "do not", "never mind")) or (
        words & {"no", "nope", "nah", "cancel", "stop", "abort", "negative", "wait"}
    ):
        return False
    if n in _YES or words & {"yes", "yeah", "yep", "yup", "sure", "proceed", "confirm", "confirmed",
                             "affirmative", "absolutely", "okay", "ok"} or any(
        p in n for p in ("go ahead", "do it", "please do", "go for it", "sounds good", "send it")
    ):
        return True
    return None


WAKE_LEAD_WORDS = 5  # the recording starts ~2 s before the wake word, so a few stray words may come first


def _wake_phrase_end(text: str, word: str = "jarvis") -> int | None:
    """Index just after "hey/hi Jarvis" if it occurs within the first few words, else None."""
    from rapidfuzz import fuzz

    toks = list(re.finditer(r"[A-Za-z']+", text))
    for i in range(min(len(toks) - 1, WAKE_LEAD_WORDS)):
        if toks[i].group().lower() in {"hey", "hi", "hay", "okay", "ok"} and any(fuzz.ratio(toks[i + 1].group().lower(), w) >= 70 for w in (word,)):
            return toks[i + 1].end()
    return None


def starts_with_wake_word(text: str, word: str = "jarvis") -> bool:
    """"Hey/Hi Jarvis" near the start counts (a bare "Jarvis" or one buried deep in a sentence does not)."""
    return _wake_phrase_end(text, word) is not None


def mentions_wake_word(text: str, word: str = "jarvis") -> bool:
    """Did the speaker actually say the wake word? Tolerates how a recogniser mangles it ("Jarvus", "Jarvas", "service")."""
    from rapidfuzz import fuzz

    return any(fuzz.ratio(tok, w) >= 70 for w in (word,) for tok in re.findall(r"[a-z']+", text.lower()))


# Words a sentence cannot sensibly end on. If you stop right after one, you paused to think; you did not finish.
_DANGLING = frozenset(
    "and but or to the a an of with for my your um uh umm uhh er because if when while".split()
)


def looks_unfinished(text: str) -> bool:
    """Did the speaker probably pause mid-sentence ("open the", "set a timer for", "call mom and")?"""
    t = text.strip().lower()
    if not t or t[-1] in ".?!":
        return False
    words = re.findall(r"[a-z']+", t)
    if len(words) < 2:
        return False
    return t[-1] in ",-\u2026" or words[-1] in _DANGLING


_OPEN_ENDERS = _DANGLING | frozenset(
    "i'm you're we're they're it's that's he's she's there's what's i'll you'll we'll i'd you'd we'd i've you've we've "
    "is are was were am will would could should can gonna going so then also like just really actually basically".split()
)


def is_trailing_fragment(text: str) -> bool:
    """A sentence that stops on a word that cannot end one ("I'm going to", "If you're", "and the"): half a thought, not a request.

    Heard after the extra wait for the rest, it was never finished, so Jarvis lets it go instead of answering it."""
    t = text.strip().lower().rstrip(".,\u2026-\u2013 ")
    if not t or t[-1] in "?!":
        return False
    words = re.findall(r"[a-z']+", t)
    return bool(words) and words[-1].replace("\u2019", "'") in _OPEN_ENDERS


def needs_clarification(avg_logprob: float | None, text: str, threshold: float) -> bool:
    """Is the recogniser unsure enough about a real request that it is worth asking "did you say ...?" first?"""
    return avg_logprob is not None and avg_logprob < threshold and len(text.split()) >= 2
