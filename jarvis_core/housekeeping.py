"""Transcript retention and other small chores that keep ``~/.jarvis`` tidy."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from jarvis_core import paths
from jarvis_core.logging_setup import redact

log = logging.getLogger(__name__)

PRUNE_EVERY_S = 6 * 3600


class TranscriptLog:
    """Daily JSONL of what was said, kept ``retention_days`` days for debugging (0 turns it off)."""

    def __init__(self, retention_days: int, directory: Path | None = None, clock=time.time) -> None:
        self.retention_days = retention_days
        self.dir = Path(directory) if directory else paths.transcripts_dir()
        self._clock = clock
        self._last_prune = 0.0

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    def _file(self, now: float) -> Path:
        return self.dir / f"{datetime.fromtimestamp(now).strftime('%Y-%m-%d')}.jsonl"

    async def on_turn_done(self, user_text: str, reply: str) -> None:
        if not self.enabled:
            return
        now = self._clock()
        rec = {"ts": round(now, 1), "user": redact(user_text)[:2000], "jarvis": redact(reply)[:2000]}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._file(now), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if now - self._last_prune > PRUNE_EVERY_S:
                self.prune()
        except OSError:
            log.debug("transcript write failed", exc_info=True)

    def prune(self) -> int:
        """Delete transcript files older than the retention window (all of them when retention is 0)."""
        self._last_prune = self._clock()
        cutoff = self._clock() - self.retention_days * 86400
        removed = 0
        if not self.dir.exists():
            return 0
        for f in self.dir.glob("*.jsonl"):
            try:
                if self.retention_days <= 0 or f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    async def on_ready(self) -> None:
        self.prune()
