"""What goes on the screen versus what is said aloud, by the kind of question asked.

Voice stays short and natural. Anything that is better read than heard (code, working, tables, step lists) goes in the
chat card as markdown. The model marks it with ``<screen>...</screen>``; a stray fenced code block is treated the same way.
"""

from __future__ import annotations

import re

from jarvis_core.brain.mathfmt import render_math

OPEN, CLOSE, FENCE = "<screen>", "</screen>", "```"

_LANGS = r"python|py|javascript|js|typescript|ts|swift|java|kotlin|rust|golang|go|c\+\+|cpp|c#|csharp|ruby|php|bash|shell|zsh|sql|html|css|json|yaml|regex|react|node|sh"
_CODE = re.compile(
    rf"\b(?:write|create|make|give|show|generate|build|fix|debug|refactor|implement|code|script|convert|translate)\b.*"
    rf"\b(?:code|script|function|class|program|regex|query|snippet|one-?liner|algorithm|component|endpoint|{_LANGS})\b"
    rf"|\bhow (?:do|can|would) (?:i|you|we) .*\b(?:in|with|using) (?:{_LANGS})\b"
    rf"|\b(?:regex|sql query|bash command|shell command|terminal command|git command)\b|\bwhat does this (?:code|function|script)\b"
    rf"|\bcode (?:for|to|that)\b|\bexplain this (?:code|function|error|stack ?trace)\b",
    re.I,
)
_MATH = re.compile(
    r"\b(?:solve|calculate|compute|evaluate|simplify|factori[sz]e|expand|differentiate|integrate|derivative|integral|equation|"
    r"limit of|probability|square root|cube root|logarithm|matrix|determinant|percent(?:age)?|compound interest|prove)\b"
    r"|\d\s*[-+*/^×÷x]\s*\d|\d+\s*%\s*of\b|\bwhat(?:'s| is) \d|\b(?:sin|cos|tan|log|ln|sqrt)\s*\(?\d",
    re.I,
)
_TABLE = re.compile(r"\b(?:compare|comparison|versus|vs\.?|difference between|differences between|pros and cons|table of|side by side)\b", re.I)
_STEPS = re.compile(
    r"\b(?:step[- ]by[- ]step|steps? to|walk me through|how (?:do|can|should) (?:i|we)|how to|tutorial|recipe for|checklist|guide to|set ?up)\b",
    re.I,
)

FORMAT_HINTS = {
    "code": (
        "This is a coding request. Say in one or two sentences what the code does, then put the code in a <screen> block as a fenced "
        "block with its language, followed by at most three short bullet notes. Do not read code aloud and do not save it to a file "
        "unless asked."
    ),
    "math": (
        "This is a maths request. Use the calculate tool for any arithmetic beyond the trivial, and for algebra or calculus. Say the "
        "answer and the idea in one or two sentences. Then, in a <screen> block, show the working as short numbered steps with the "
        "maths in LaTeX ($...$ inline, $$...$$ for a key equation on its own line), ending with a line **Answer:** ... "
        "For a quick sum, just say the result and skip the screen block."
    ),
    "table": (
        "This is a comparison. Say your verdict in one or two sentences, and put a markdown table in a <screen> block "
        "(one row per option, short cells), plus one line of recommendation under it."
    ),
    "steps": (
        "This is a how-to. Say the gist in one sentence, and put the steps as a numbered markdown list in a <screen> block, each step "
        "one short line, with any commands in inline code or fenced blocks."
    ),
}


def classify_question(text: str) -> str:
    """'code', 'math', 'table', 'steps' or 'chat'. Order matters: code beats steps, maths beats chat."""
    t = (text or "").strip()
    if not t:
        return "chat"
    if _CODE.search(t):
        return "code"
    if _TABLE.search(t):
        return "table"
    if _MATH.search(t):
        return "math"
    if _STEPS.search(t):
        return "steps"
    return "chat"


def format_hint(text: str) -> str:
    kind = classify_question(text)
    return FORMAT_HINTS.get(kind, "")


def _partial_suffix(buf: str, needles: tuple[str, ...]) -> int:
    """Length of the longest tail of ``buf`` that could be the start of one of ``needles``."""
    best = 0
    for n in needles:
        for k in range(min(len(n) - 1, len(buf)), 0, -1):
            if buf.endswith(n[:k]):
                best = max(best, k)
                break
    return best


class ScreenSplitter:
    """Splits a streamed reply into the part to speak and the part to show."""

    def __init__(self) -> None:
        self._buf = ""
        self._mode = "speech"  # speech | tag | fence
        self._screen: list[str] = []

    @property
    def screen(self) -> str:
        return render_math("".join(self._screen)).strip()

    @property
    def has_screen(self) -> bool:
        return bool(self._screen) or self._mode != "speech"

    def feed(self, piece: str) -> str:
        self._buf += piece
        spoken: list[str] = []
        while True:
            if self._mode == "speech":
                hits = [i for i in (self._buf.find(OPEN), self._buf.find(FENCE)) if i != -1]
                if not hits:
                    keep = _partial_suffix(self._buf, (OPEN, FENCE))
                    cut = len(self._buf) - keep
                    spoken.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                i = min(hits)
                spoken.append(self._buf[:i])
                self._buf = self._buf[i:]
                if self._buf.startswith(OPEN):
                    self._buf = self._buf[len(OPEN) :]
                    self._mode = "tag"
                else:
                    self._mode = "fence"
            elif self._mode == "tag":
                j = self._buf.find(CLOSE)
                if j == -1:
                    keep = _partial_suffix(self._buf, (CLOSE,))
                    cut = len(self._buf) - keep
                    self._screen.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                self._screen.append(self._buf[:j] + "\n\n")
                self._buf = self._buf[j + len(CLOSE) :]
                self._mode = "speech"
            else:  # a bare fenced block outside <screen>
                nl = self._buf.find("\n")
                j = self._buf.find(FENCE, nl) if nl != -1 else -1
                if j == -1:
                    break
                end = j + len(FENCE)
                self._screen.append(self._buf[:end] + "\n\n")
                self._buf = self._buf[end:]
                self._mode = "speech"
        return "".join(spoken)

    def flush(self) -> str:
        """End of stream: returns any leftover speech; an unclosed block is kept as screen text."""
        rest, self._buf = self._buf, ""
        if self._mode == "speech":
            return rest
        self._screen.append(rest)
        if self._mode == "fence" and not rest.rstrip().endswith(FENCE):
            self._screen.append("\n```")
        self._mode = "speech"
        return ""
