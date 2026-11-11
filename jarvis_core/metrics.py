"""Timings, kept on this Mac, so speed work is guided by numbers and not by feel.

Each line of ``~/.jarvis/metrics.jsonl`` is one measurement: a stage name, milliseconds and the time. No words from any
conversation are ever written here. ``jarvisd stats`` summarises it.

Stages:

* ``stt_ms``: speech to text for one utterance.
* ``think_ms``: from the end of your request to the model's reply starting to speak (model, tools and first sentence).
* ``to_first_audio_ms``: from the moment you stopped talking to the first sound, the delay you actually feel.
* ``turn_ms``: the whole turn, including everything said.
* ``cpu_pct`` and ``rss_mb``: this process, sampled while idle.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from jarvis_core import paths

log = logging.getLogger(__name__)

MAX_BYTES = 2_000_000
STAGES = ("to_first_audio_ms", "stt_ms", "think_ms", "turn_ms", "cpu_pct", "rss_mb")


class Metrics:
    def __init__(self, path: Path | None = None, clock=time.time) -> None:
        self.path = path or paths.home() / "metrics.jsonl"
        self._clock = clock

    def record(self, stage: str, value: float, **extra: Any) -> None:
        """Append one measurement. Never raises: a full disk must not stop Jarvis answering."""
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.path.with_suffix(".old"))
            with self.path.open("a") as f:
                f.write(json.dumps({"t": round(self._clock(), 3), "stage": stage, "v": round(float(value), 2), **extra}) + "\n")
            self.path.chmod(0o600)
        except OSError:
            log.debug("metrics not written", exc_info=True)

    def read(self, days: float | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cutoff = self._clock() - days * 86400 if days else None
        for p in (self.path.with_suffix(".old"), self.path):
            try:
                text = p.read_text()
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and "stage" in row and (cutoff is None or row.get("t", 0) >= cutoff):
                    rows.append(row)
        return rows

    def summary(self, days: float | None = None) -> dict[str, dict[str, float]]:
        by: dict[str, list[float]] = {}
        for r in self.read(days):
            by.setdefault(r["stage"], []).append(float(r["v"]))
        out: dict[str, dict[str, float]] = {}
        for stage, vals in by.items():
            vals.sort()
            out[stage] = {"n": len(vals), "p50": _pct(vals, 50), "p95": _pct(vals, 95), "max": vals[-1]}
        return out


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def format_summary(summary: dict[str, dict[str, float]]) -> str:
    if not summary:
        return "No measurements yet. Use Jarvis for a while, then run this again."
    lines = [f"{'stage':<20}{'count':>7}{'median':>10}{'95th':>10}{'worst':>10}"]
    ordered = [s for s in STAGES if s in summary] + sorted(set(summary) - set(STAGES))
    for stage in ordered:
        s = summary[stage]
        lines.append(f"{stage:<20}{int(s['n']):>7}{s['p50']:>10.0f}{s['p95']:>10.0f}{s['max']:>10.0f}")
    return "\n".join(lines)
