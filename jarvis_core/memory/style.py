"""Writing-style cards, learned from the user's own sent messages. Pure statistics: nothing leaves the Mac."""

from __future__ import annotations

import re
from collections import Counter

from jarvis_core.comms.drafter import APP_LABEL

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_SIGNOFFS = ("cheers", "thanks", "thank you", "regards", "best", "ttyl", "see you", "talk soon")
MIN_SAMPLES = 3


def style_card(app: str, samples: list[str]) -> str | None:
    msgs = [s.strip() for s in samples if s and s.strip()]
    if len(msgs) < MIN_SAMPLES:
        return None
    words = [len(m.split()) for m in msgs]
    avg = sum(words) / len(words)
    length = "very short" if avg < 5 else "short" if avg < 12 else "medium-length" if avg < 30 else "long"
    letters = [m for m in msgs if re.search(r"[a-z]", m, re.I)]
    lower = sum(1 for m in letters if m[0].islower()) / max(len(letters), 1)
    case = "usually starts in lowercase" if lower > 0.6 else "capitalises properly"
    emoji = sum(len(_EMOJI.findall(m)) for m in msgs) / len(msgs)
    emo = "uses emoji often" if emoji >= 0.8 else "uses an occasional emoji" if emoji >= 0.15 else "rarely uses emoji"
    punct = sum(1 for m in msgs if re.search(r"[.!?]$", m)) / len(msgs)
    end = "ends messages with punctuation" if punct > 0.6 else "usually drops the final full stop"
    bang = sum(m.count("!") for m in msgs) / len(msgs)
    tone = "enthusiastic (uses exclamation marks)" if bang >= 0.7 else "even in tone"
    low = " ".join(msgs).lower()
    signs = Counter(s for s in _SIGNOFFS if s in low)
    sign = f" Sometimes says '{signs.most_common(1)[0][0]}'." if signs else ""
    abbr = " Uses casual abbreviations." if re.search(r"\b(?:u|ur|pls|plz|thx|ok|k|gonna|wanna|lol)\b", low) else ""
    label = APP_LABEL.get(app, app)
    return f"On {label} the user writes {length} messages (about {avg:.0f} words), {case}, {end}, {emo}, {tone}.{abbr}{sign}"


def learn_style(memory, app: str, samples: list[str]) -> bool:
    card = style_card(app, samples)
    if card is None or memory is None:
        return False
    try:
        memory.upsert_style(app, card)
    except Exception:
        return False
    return True
