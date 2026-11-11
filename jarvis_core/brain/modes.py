"""Decide how hard to think, and in which way, for each request.

Every turn is classified by *kind* (what is being asked) and *complexity* (how much work it is). Those pick a reasoning
*technique* (direct, chain of thought, least-to-most, tree of thoughts, graph of thoughts, with a self-check where mistakes are
costly), a spoken-length budget, sampling settings and a thinking budget. Thinking happens privately inside ``<think>`` tags
that never reach the speaker or the screen. Simple requests skip it so quick answers stay quick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jarvis_core.brain.display import FORMAT_HINTS, classify_question

Kind = str  # chat | action | lookup | explain | code | math | analysis | plan | troubleshoot | draft | summarize | creative

_ACTION_VERBS = frozenset(
    "set open close quit play pause resume turn remind make start stop text message send call search find take save add remove "
    "delete move snap put launch switch mute unmute lock schedule book reply connect disconnect skip shuffle create copy change fix "
    "edit replace update correct paste type".split()
)
_LEAD = frozenset("please can could would you jarvis hey hi okay ok just then now and also".split())

_PATTERNS: list[tuple[Kind, re.Pattern[str]]] = [
    ("summarize", re.compile(r"\b(?:summari[sz]e|tl;?dr|recap|what did .{1,30} (?:say|write|send)|catch me up|what(?:'s| is) in (?:my|the) (?:inbox|messages|emails?))\b", re.I)),
    ("draft", re.compile(r"\b(?:draft|compose|write (?:a|an|the|my)? ?(?:email|message|reply|note|text|post|letter|bio|summary|caption|cover letter)|rewrite (?:this|that|it|my|the) ?(?:text|message|email|paragraph|sentence|note|reply|post|bio|draft)?|rephrase|proofread|polish (?:this|that|my|the)|reply to|respond to)\b", re.I)),
    ("troubleshoot", re.compile(r"\b(?:not working|doesn'?t work|isn'?t working|won'?t (?:open|start|load|connect|work|charge|sync)|keeps? (?:dropping|disconnecting|crashing|freezing|restarting)|drops?|dropping|disconnect(?:s|ing)?|laggy|lag|overheat(?:s|ing)?|too (?:loud|hot|slow)|battery (?:drain|dies)|what'?s going on|what's wrong|broken|crash(?:es|ed|ing)?|stuck|error|bug|fail(?:s|ed|ing)?|why (?:is|does|won'?t|isn'?t|can'?t)|how (?:do|can) i fix|slow|freez(?:e|ing))\b", re.I)),
    ("analysis", re.compile(r"\b(?:should i|whether (?:to|or not)|is it better to|go with|which (?:one|is better|should)|better|worth it|pros and cons|trade-?offs?|decide|decision|recommend|best (?:way|option|choice)|is it a good idea|evaluate|assess|compare|versus|vs\.?|difference between|what would you do)\b", re.I)),
    ("plan", re.compile(r"\b(?:plan|schedule my|organi[sz]e|roadmap|strategy|itinerary|prioriti[sz]e|step[- ]by[- ]step|how (?:do|can|should) (?:i|we)|how to|walk me through|set ?up|checklist|workflow|break (?:this|it) down)\b", re.I)),
    ("creative", re.compile(r"\b(?:brainstorm|ideas? for|come up with|suggest (?:some )?names?|name (?:for|my)|story|poem|joke|slogan|tagline|invent|imagine|write me a)\b", re.I)),
    ("explain", re.compile(r"\b(?:explain|why (?:do|did|are|was|were|is)\b(?! you)|how (?:does|did) \w+|how do (?!you\b|i\b)\w+ \w+|what(?:'s| is) the (?:difference|point|idea)|teach me|eli5|what does .{1,40} mean|tell me about|how come)", re.I)),
    ("lookup", re.compile(r"\b(?:who (?:is|was|won|wrote)|when (?:is|was|did)|where (?:is|was)|what(?:'s| is) (?:the )?(?:capital|population|weather|time|date|price|score|definition)|how (?:many|much|old|tall|far|long)|define|meaning of|latest|current|today)\b", re.I)),
]

_SMALLTALK = re.compile(r"^\W*(?:hi|hello|hey|yo|thanks?|thank you|cheers|good (?:morning|afternoon|evening|night)|how are you|how's it going|what's up|sup)\b", re.I)

_HARD_WORDS = re.compile(
    r"\b(?:carefully|in depth|thorough(?:ly)?|detailed|think|reason|prove|optimi[sz]e|architecture|trade-?offs?|edge cases?|"
    r"constraints?|multiple|several|and also|as well as|on the other hand|then|after that|first .{1,60} then)\b",
    re.I,
)

LOW, MEDIUM, HIGH = "low", "medium", "high"


@dataclass(frozen=True)
class TurnPlan:
    kind: Kind
    complexity: str
    technique: str  # direct | cot | ltm | tot | got
    verify: bool
    speech_sentences: str  # how long the spoken part should be
    max_sentences: int  # hard cap on sentences actually voiced (the card still shows the whole reply)
    think_words: int  # private thinking budget; 0 = do not think
    temperature: float
    max_tokens: int

    @property
    def thinks(self) -> bool:
        return self.think_words > 0 and self.technique != "direct"


def _is_action(text: str) -> bool:
    words = re.findall(r"[a-z']+", text.lower())
    while words and words[0] in _LEAD:
        words.pop(0)
    return bool(words) and words[0] in _ACTION_VERBS


_QUANT_Q = re.compile(r"\b(?:when|how (?:long|far|many|much|fast|old|tall)|what time|catch(?:es)? up|meet|average|total|remaining|left over|each|per|split|ratio|break[- ]even)\b", re.I)


def _word_problem(t: str) -> bool:
    """A story with numbers in it and a quantity being asked for ("a train leaves at 3:15 going 80 km/h...")."""
    return len(re.findall(r"\d+(?:[.,:]\d+)?", t)) >= 2 and bool(_QUANT_Q.search(t)) and len(t.split()) >= 8


def classify_kind(text: str, previous: Kind = "chat") -> Kind:
    t = (text or "").strip()
    if not t:
        return "chat"
    if _SMALLTALK.match(t) and len(t.split()) <= 6:
        return "chat"
    base = classify_question(t)  # code / math / table / steps / chat
    if base == "code":
        return "code"
    if base == "math":
        return "math"
    if _is_action(t) and not re.search(r"\b(?:why|how come|explain|should i)\b", t, re.I):
        return "action"
    if _word_problem(t):
        return "math"
    if base == "table":
        return "analysis"
    for kind, rx in _PATTERNS:
        if rx.search(t):
            return kind
    if base == "steps":
        return "plan"
    if len(t.split()) <= 5 and previous not in ("chat", "action"):
        return previous  # "and the second one?", "why?", "make it shorter": same kind of conversation as before
    return "chat"


def complexity_of(text: str, kind: Kind) -> str:
    t = text or ""
    words = len(t.split())
    score = 0
    score += 1 if words > 14 else 0
    score += 1 if words > 30 else 0
    score += 1 if t.count(",") >= 3 else 0
    score += min(2, len(_HARD_WORDS.findall(t)))
    score += 1 if t.count("?") > 1 or len(re.findall(r"\band\b", t, re.I)) >= 2 else 0
    score += 1 if kind in ("analysis", "plan", "troubleshoot") else 0
    if kind in ("chat", "action", "lookup"):
        score = max(0, score - 1)
    return LOW if score <= 1 else MEDIUM if score <= 3 else HIGH


# kind -> (speech length, sampling temperature, base max_tokens)
_PROFILE: dict[Kind, tuple[str, int, float, int]] = {
    "chat": ("one or two short sentences", 3, 0.65, 350),
    "action": ("one short line", 2, 0.2, 300),
    "lookup": ("one or two sentences", 3, 0.25, 350),
    "explain": ("two to four spoken sentences", 5, 0.45, 700),
    "code": ("one or two sentences", 2, 0.2, 1400),
    "math": ("one or two sentences", 2, 0.1, 900),
    "analysis": ("two or three sentences", 3, 0.45, 900),
    "plan": ("one or two sentences", 2, 0.4, 1000),
    "troubleshoot": ("one or two sentences: the cause, then the fix", 2, 0.3, 1400),
    "draft": ("one or two sentences about the draft", 2, 0.6, 900),
    "summarize": ("two or three sentences, most important first", 4, 0.3, 700),
    "creative": ("two or three sentences", 4, 0.9, 900),
}


def _technique(kind: Kind, cx: str) -> tuple[str, bool]:
    """(technique, verify)."""
    # Thinking costs a second or two before the first word, so quick requests skip it. Maths always gets it: that is where a
    # wrong answer is most likely and most costly.
    if kind in ("chat", "action", "lookup", "draft", "summarize"):
        return ("cot" if cx == HIGH else "direct"), (kind == "draft" and cx != LOW)
    if kind == "math":
        return "cot", True
    if kind == "code":
        return ("ltm" if cx == HIGH else "cot" if cx == MEDIUM else "direct"), cx != LOW
    if kind == "troubleshoot":
        return ("got" if cx == HIGH else "cot" if cx == MEDIUM else "direct"), cx != LOW
    if kind == "analysis":
        return ("got" if cx == HIGH else "tot" if cx == MEDIUM else "direct"), cx != LOW
    if kind == "plan":
        return ("got" if cx == HIGH else "ltm" if cx == MEDIUM else "direct"), cx == HIGH
    if kind == "creative":
        return ("tot" if cx != LOW else "direct"), False
    if kind == "explain":
        return ("cot" if cx != LOW else "direct"), False
    return "direct", False


_THINK_WORDS = {LOW: 70, MEDIUM: 140, HIGH: 260}


def plan_turn(text: str, previous: Kind = "chat", *, reasoning: bool = True, max_think_words: int = 260) -> TurnPlan:
    kind = classify_kind(text, previous)
    cx = complexity_of(text, kind)
    technique, verify = _technique(kind, cx)
    speech, cap, temp, tokens = _PROFILE[kind]
    think = 0
    if reasoning and technique != "direct":
        think = min(_THINK_WORDS[cx], max_think_words)
    return TurnPlan(
        kind=kind, complexity=cx, technique=technique, verify=verify and reasoning, speech_sentences=speech, max_sentences=cap,
        think_words=think, temperature=temp, max_tokens=tokens + int(think * 1.6),
    )


_TECHNIQUES = {
    "cot": (
        "Think step by step: restate what is really being asked, list what is known, work through it one step at a time, and "
        "check the result (units, edge cases, does it answer exactly the question?)."
    ),
    "ltm": (
        "Work least-to-most: break the request into the smallest sub-questions, in the order they depend on each other, answer "
        "each using the earlier answers, then combine them."
    ),
    "tot": (
        "Explore before committing: lay out three genuinely different options (A, B, C), judge each against what the user is trying "
        "to achieve and what is known about them (time, cost, risk, preferences), drop the weakest, and pick one. Note the single "
        "reason that decided it and the main trade-off."
    ),
    "got": (
        "Work it as a graph of thoughts: list the sub-problems as nodes, note which depend on which, solve them in order, merge "
        "conclusions that overlap, resolve any that conflict, then do one consistency check across the whole."
    ),
}

_VERIFY = "Before you reply, check your own answer once: is it correct, complete, and in the format asked? Fix it first if not."

_KIND_RULES = {
    "action": "The user wants something done: call the tool, then confirm in one short line. Do not explain.",
    "lookup": "Answer directly with the fact first. If it could have changed since your training (news, prices, scores, weather, 'latest'), use a tool to check; if you cannot, say you are unsure.",
    "explain": "Explain it like a clever friend: the core idea first, then one concrete example or analogy. Skip history and caveats unless they matter.",
    "analysis": "Give a real recommendation, not a survey: what you would do and the one reason why.",
    "plan": "Give a plan the user can start on now: the first step is specific and doable today. Keep the rest short.",
    "troubleshoot": "Look before you answer (read-only tools, read_screen_code for code on screen), then name the one most likely cause and the fix. Skip lists of other things it could be. Never change a setting just to test a theory; ask first.",
    "draft": "Write the draft in the user's voice, show it on screen, and say in one sentence what it does. Ask before sending anything; you only ever draft.",
    "summarize": "Summarise, never recite. Lead with what matters or is urgent, group by person or topic, and offer to read details.",
    "creative": "Be inventive and specific, not generic. Offer a few distinct options rather than one safe one.",
    "chat": "Keep it natural and brief. Match their mood.",
    "math": "Get the numbers right: use the calculate tool for anything beyond a trivial sum, and say the answer first.",
    "code": "Correct, idiomatic, minimal code that runs as written. If it is about code on their screen, read it first with read_screen_code and keep their approach.",
}

_SCREEN_KIND = {"code": "code", "math": "math", "analysis": "table", "plan": "steps", "troubleshoot": "steps", "draft": ""}


def plan_block(plan: TurnPlan, *, user_text: str = "", notes: str = "") -> str:
    """The per-turn part of the system prompt."""
    lines = [f"This turn: {plan.kind} request, {plan.complexity} complexity. Speak {plan.speech_sentences}, and no more."]
    rule = _KIND_RULES.get(plan.kind)
    if rule:
        lines.append(rule)
    if notes:
        lines.append(
            "Your own private working notes for this request are below. Use them, but re-check anything that looks off, and never "
            f"mention or quote them.\n<notes>\n{notes.strip()}\n</notes>"
        )
    if plan.verify:
        lines.append(_VERIFY)
    hint = FORMAT_HINTS.get(_SCREEN_KIND.get(plan.kind, ""), "") or (format_screen_hint(user_text) if user_text else "")
    if hint:
        lines.append(hint)
    return "\n".join(lines)


def deliberation_messages(plan: TurnPlan, user_text: str, *, context: str = "", recent: list[dict] | None = None) -> list[dict]:
    """A private first pass: the model works the problem out in notes that are never spoken or shown."""
    system = (
        "You are the private reasoning step of a personal voice assistant. Work the user's request out and write WORKING NOTES only: "
        "terse, no pleasantries, no final spoken answer.\n"
        f"{_TECHNIQUES.get(plan.technique, _TECHNIQUES['cot'])}\n"
        f"Stay under {plan.think_words} words. Do any arithmetic carefully and double-check it. Finish with a line starting "
        "'Conclusion:' giving the answer or recommendation in one or two sentences.\n"
        + (f"Context:\n{context}\n" if context else "")
    )
    tail = [m for m in (recent or []) if m.get("role") in ("user", "assistant") and m.get("content")][-4:]
    return [{"role": "system", "content": system}, *tail, {"role": "user", "content": user_text}]


def format_screen_hint(text: str) -> str:
    from jarvis_core.brain.display import format_hint

    return format_hint(text)
