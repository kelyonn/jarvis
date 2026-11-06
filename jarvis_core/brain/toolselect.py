"""Pick which tool categories to offer the model for a request.

Offering all ~90 tools costs ~7k prompt tokens per turn and makes tool choice worse. Instead we always offer a small
core, add categories whose keywords appear in the request (plus the ones used in the last few turns, so follow-ups like
"make it shorter" still see the messaging tools), and fall back to everything when nothing matches.
"""

from __future__ import annotations

import re

CORE = frozenset({"apps", "web", "memory", "sysinfo"})
STICKY_TURNS = 2

_KEYWORDS: dict[str, str] = {
    "messaging": r"messag|text (?:him|her|them|my|mom|dad)|whatsapp|imessage|e-?mail|gmail|inbox|linkedin|reply|draft|\bdm\b|\bvip|"
                 r"unread|tell (?:him|her|them)|let .{1,20} know|write to|compose",
    "windows": r"window|snap|half|fullscreen|full screen|maximi[sz]e|minimi[sz]e|tile|left side|right side|move this",
    "files": r"\bfiles?\b|\bcopy (?:the |this |that )?(?:file|folder)|duplicate|folder|document|download|desktop|trash|finder|\bpdf\b|rename|directory|find my",
    "media": r"music|\bplay\b|pause|song|track|spotify|volume|\bmute|louder|quieter|sound|podcast|\bskip|next|previous|playlist",
    "display": r"brightness|dark mode|light mode|lock (?:the |my )?(?:screen|mac)|display|screen off",
    "power": r"battery|\bsleep\b|restart|shut ?down|reboot|empty (?:the )?trash|awake|caffeinate",
    "connectivity": r"wi-?fi|bluetooth|airpods|headphones|headset|connect to|disconnect",
    "notes": r"\bnotes?\b|jot|write (?:this )?down",
    "reminders": r"remind|reminder|to-?do|checklist",
    "timers": r"timer|alarm|countdown|stopwatch|wake me",
    "browser": r"\btabs?\b|browser|chrome|safari|webpage|website|web page|search for",
    "clipboard": r"clipboard|copied|\bpaste|\bcopy\b",
    "terminal": r"terminal|command|shell|script|\bgit\b|\bnpm\b|\bbrew\b|\bpip\b|\bssh\b|\brun \S",
    "routines": r"\bmode\b|routine|workspace|setup|set up my",
    "alerts": r"alert|missed|snooze|notification|disturb|what did i miss",
    "capture": r"screen ?shot|capture (?:the |my |this )?(?:screen|window)",
    "focus": r"\bfocus\b|do not disturb|don'?t disturb|\bdnd\b",
    "calendar": r"calendar|appointment|meeting|schedule|\bevent|book (?:a|an|me)\b",
    "drive": r"\bdrive\b|google docs?|\bdocs?\b|spreadsheet|\bsheets?\b|slides|presentation|my files in",
    "vision": r"screen|\bsee\b|look at|this error|explain this|computer use|click|on my display|what am i looking",
    "math": r"calculat|comput|solve|equation|integral|derivative|differentiat|simplif|factori|expand|limit of|\d\s*[-+*/^x×÷]\s*\d|\d\s*%|percent|square root|sqrt|cube root|log\b|\bsin\b|\bcos\b|\btan\b|how much is|what(?:'s| is) \d",
    "briefing": r"brief|weather|forecast|rain|market|stock|nifty|sensex|nasdaq|s&p|bitcoin|btc|ethereum|\beth\b|news|headline|"
                r"calendar|meeting|schedule|agenda|today|tomorrow|event",
}
_COMPILED = {cat: re.compile(rx, re.I) for cat, rx in _KEYWORDS.items()}


def match_categories(text: str) -> set[str]:
    return {cat for cat, rx in _COMPILED.items() if rx.search(text or "")}


def select_categories(text: str, sticky: set[str] | None = None) -> set[str] | None:
    """Categories to offer, or ``None`` for "all tools" (empty text, or no keyword matched)."""
    if not text or not text.strip():
        return None
    matched = match_categories(text)
    if not matched:
        return None
    return set(CORE) | matched | (sticky or set())
