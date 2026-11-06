"""Turns a flat sentence into something that sounds spoken: clause pacing, breaths and gentle speed variation.

A TTS engine reading a whole sentence at one constant rate is what makes it sound monotone. People slow down on
questions and short warm replies, speed through asides, pause at commas and take a small breath between sentences.
This module only plans that (pure and deterministic, so it is unit-tested); the engines render each clause.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

import numpy as np

# Pause (milliseconds) after a clause that ends with this mark.
PAUSE_MS = {",": 150, ";": 240, ":": 220, "—": 230, "…": 380, ".": 300, "?": 340, "!": 260, "": 180}
MAX_JITTER = 0.035  # +/- speed variation so consecutive clauses never share an identical cadence
MIN_CLAUSE_WORDS = 3  # shorter clauses are merged into a neighbour; very short fragments sound choppy

_SPLIT = re.compile(r"(?<=[,;:—…])\s+|\s+—\s+|\s+-\s+")
_END = re.compile(r"([.?!…]+)[\"')\]]*\s*$")


@dataclass(frozen=True)
class Clause:
    text: str
    speed: float  # multiplier on the engine's base speed
    pause_ms: int  # silence after this clause


def _jitter(text: str, salt: int) -> float:
    """Stable per-clause variation in [-MAX_JITTER, +MAX_JITTER] (same text -> same delivery)."""
    h = zlib.crc32(f"{salt}:{text}".encode()) & 0xFFFF
    return (h / 0xFFFF * 2 - 1) * MAX_JITTER


def _merge_short(parts: list[str]) -> list[str]:
    out: list[str] = []
    for p in parts:
        if out and len(p.split()) < MIN_CLAUSE_WORDS:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    if len(out) > 1 and len(out[0].split()) < MIN_CLAUSE_WORDS:
        out[1] = f"{out[0]} {out[1]}"
        out = out[1:]
    return out


def _mark(clause: str) -> str:
    c = clause.rstrip("\"')] ")
    if c.endswith("..."):
        return "…"
    if c.endswith("…"):
        return "…"
    if c[-1:] in {",", ";", ":", "—", ".", "?", "!"}:
        return c[-1]
    return ""


def plan(sentence: str, base_speed: float = 1.0) -> list[Clause]:
    """Split one sentence into clauses with a speed and a trailing pause for each."""
    text = " ".join(sentence.split())
    if not text:
        return []
    parts = _merge_short([p.strip() for p in _SPLIT.split(text) if p.strip()])
    end = _END.search(text)
    is_question = bool(end and "?" in end.group(1))
    is_exclaim = bool(end and "!" in end.group(1))
    short_reply = len(text.split()) <= 5  # "Of course, sir." / "Done." read a touch slower and warmer
    clauses: list[Clause] = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        mark = _mark(part)
        speed = base_speed * (1.0 + _jitter(part, i))
        if short_reply:
            speed *= 0.95
        if last and is_question:
            speed *= 0.96
        elif last and is_exclaim:
            speed *= 1.03
        elif not last and mark == ",":
            speed *= 1.02  # lead-ins and asides run slightly quicker; the main clause lands slower
        elif last and len(parts) > 1:
            speed *= 0.98
        pause = PAUSE_MS.get(mark, PAUSE_MS[""])
        if last:
            pause = PAUSE_MS.get(mark, PAUSE_MS["."]) if mark else PAUSE_MS["."]
        clauses.append(Clause(part, round(speed, 3), pause))
    return clauses


def silence(ms: int, sample_rate: int) -> np.ndarray:
    return np.zeros(int(sample_rate * ms / 1000), dtype=np.float32)


def fade_edges(audio: np.ndarray, sample_rate: int, ms: int = 6) -> np.ndarray:
    """Tiny fades so joining clauses never clicks."""
    n = min(int(sample_rate * ms / 1000), len(audio) // 2)
    if n <= 0:
        return audio
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    audio = audio.copy()
    audio[:n] *= ramp
    audio[-n:] *= ramp[::-1]
    return audio


def trim_silence(audio: np.ndarray, threshold: float = 0.004, keep_ms: int = 25, sample_rate: int = 24000) -> np.ndarray:
    """Engines pad clauses with variable silence; trim it so the planned pauses are the only pauses."""
    if audio.size == 0:
        return audio
    idx = np.flatnonzero(np.abs(audio) > threshold)
    if idx.size == 0:
        return audio[:0]
    keep = int(sample_rate * keep_ms / 1000)
    return audio[max(0, idx[0] - keep): idx[-1] + keep]


def assemble(pieces: list[tuple[np.ndarray, int]], sample_rate: int) -> np.ndarray:
    """Join rendered clauses with their pauses. The last pause is shortened: the next sentence adds its own lead-in."""
    out: list[np.ndarray] = []
    for i, (audio, pause_ms) in enumerate(pieces):
        audio = fade_edges(trim_silence(audio, sample_rate=sample_rate), sample_rate)
        if audio.size == 0:
            continue
        out.append(audio)
        out.append(silence(pause_ms if i < len(pieces) - 1 else min(pause_ms, 220), sample_rate))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)
