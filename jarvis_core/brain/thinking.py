"""Remove private reasoning (<think>...</think>) from a streamed reply before anything is spoken, shown or remembered."""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_OPENERS = ("<think>", "<thinking>")
_CLOSERS = ("</think>", "</thinking>")


def _partial(buf: str, needles: tuple[str, ...]) -> int:
    best = 0
    for n in needles:
        for k in range(min(len(n) - 1, len(buf)), 0, -1):
            if buf.endswith(n[:k]):
                best = max(best, k)
                break
    return best


class ThinkFilter:
    """Streaming filter. ``feed`` returns the visible text; ``flush`` finishes the stream.

    An unclosed think block (the model ran out of tokens mid-thought) is never spoken, except that if it produced no answer at
    all, its last paragraph is used as the answer so the user is not left with silence.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._inside = False
        self._thought: list[str] = []
        self._emitted = False
        self.thought = ""

    def feed(self, piece: str) -> str:
        self._buf += piece
        out: list[str] = []
        while True:
            if not self._inside:
                hits = [(self._buf.find(o), o) for o in _OPENERS if self._buf.find(o) != -1]
                if not hits:
                    keep = _partial(self._buf, _OPENERS)
                    cut = len(self._buf) - keep
                    out.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                i, tag = min(hits)
                out.append(self._buf[:i])
                self._buf = self._buf[i + len(tag) :]
                self._inside = True
            else:
                hits = [(self._buf.find(c), c) for c in _CLOSERS if self._buf.find(c) != -1]
                if not hits:
                    keep = _partial(self._buf, _CLOSERS)
                    cut = len(self._buf) - keep
                    self._thought.append(self._buf[:cut])
                    self._buf = self._buf[cut:]
                    break
                i, tag = min(hits)
                self._thought.append(self._buf[:i])
                self._buf = self._buf[i + len(tag) :].lstrip("\n")
                self._inside = False
        text = "".join(out)
        if text.strip():
            self._emitted = True
        return text

    def flush(self) -> str:
        rest, self._buf = self._buf, ""
        if self._inside:
            self._thought.append(rest)
            self.thought = "".join(self._thought)
            self._inside = False
            if not self._emitted:
                paras = [p.strip() for p in re.split(r"\n\s*\n", self.thought) if p.strip()]
                return paras[-1] if paras else ""
            return ""
        self.thought = "".join(self._thought)
        if rest.strip():
            self._emitted = True
        return rest


def strip_think(text: str) -> str:
    f = ThinkFilter()
    return (f.feed(text) + f.flush()).strip()
