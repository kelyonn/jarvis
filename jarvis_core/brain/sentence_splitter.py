"""Turn a stream of LLM tokens into speakable sentences."""

from __future__ import annotations

import re

_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "a.m", "p.m",
    "no", "inc", "ltd", "co",
}
_END = ".?!;"


class SentenceSplitter:
    """Buffers streamed text and releases complete sentences.

    A sentence is released at ``. ? ! ;`` followed by whitespace, once it has at
    least ``min_words`` words. Short fragments are merged into the next sentence.
    """

    def __init__(self, min_words: int = 3, max_chars: int = 220) -> None:
        self.min_words = min_words
        self.max_chars = max_chars
        self._buf = ""

    def feed(self, text: str) -> list[str]:
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> str | None:
        out = self._drain(final=True)
        rest = self._buf.strip()
        self._buf = ""
        pieces = out + ([rest] if rest else [])
        joined = " ".join(p for p in pieces if p).strip()
        return joined or None

    # -- internals ----------------------------------------------------------

    def _drain(self, final: bool) -> list[str]:
        out: list[str] = []
        while True:
            cut = self._find_cut(final)
            if cut is None:
                break
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:]
            if sentence:
                out.append(sentence)
        return out

    def _find_cut(self, final: bool) -> int | None:
        buf = self._buf
        for m in re.finditer(r"[.?!;]+|\n+", buf):
            end = m.end()
            token = m.group()
            followed_by_space = end < len(buf) and buf[end].isspace()
            is_newline = token.startswith("\n")
            if not is_newline and not followed_by_space:
                if not (final and end == len(buf)):
                    continue
            candidate = buf[:end]
            if token.startswith(".") and not is_newline and self._is_abbrev_or_number(candidate):
                continue
            if len(candidate.split()) < self.min_words and not (
                token[0] in "?!" and len(candidate.split()) >= 2
            ):
                continue
            return end
        if len(buf) > self.max_chars:
            comma = buf.rfind(",", 0, self.max_chars)
            if comma > 40:
                return comma + 1
            space = buf.rfind(" ", 0, self.max_chars)
            if space > 40:
                return space
        return None

    @staticmethod
    def _is_abbrev_or_number(candidate: str) -> bool:
        m = re.search(r"([\w.]+)\.$", candidate)
        if not m:
            return False
        word = m.group(1).lower()
        return word in _ABBREV or bool(re.fullmatch(r"[a-z]", word))
