"""Short-term conversation window with idle expiry."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from jarvis_core.interfaces import ChatMsg


class ConversationHistory:
    def __init__(
        self,
        *,
        max_turns: int = 6,
        expiry_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        path: Path | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.max_turns = max_turns
        self.expiry_s = expiry_s
        self._clock = clock
        self._msgs: list[ChatMsg] = []
        self._last = clock()
        self.path = Path(path) if path else None
        self._wall = wall
        self._load()

    # A conversation survives restarts: it is written to disk (private file) and picked up again while still fresh.
    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            age = self._wall() - float(data["saved_at"])
            msgs = [m for m in data["msgs"] if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                    and not m.get("tool_calls") and m.get("content")]
        except (OSError, ValueError, KeyError, TypeError):
            return
        if 0 <= age <= self.expiry_s and msgs:
            self._msgs = msgs
            self._last = self._clock() - age
            self._trim()

    def _save(self) -> None:
        if self.path is None:
            return
        # Tool exchanges are transient; only what was actually said is kept.
        said = [m for m in self._msgs if m.get("role") in ("user", "assistant") and not m.get("tool_calls") and m.get("content")]
        try:
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"saved_at": self._wall(), "msgs": said}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass  # remembering the conversation across restarts is a nicety, never worth failing a turn

    def _expire(self) -> None:
        if self._msgs and self._clock() - self._last > self.expiry_s:
            self._msgs.clear()
            self._save()

    def messages(self) -> list[ChatMsg]:
        self._expire()
        return list(self._msgs)

    def add(self, msg: ChatMsg) -> None:
        self._expire()
        self._msgs.append(msg)
        self._last = self._clock()
        self._trim()
        self._save()

    def extend(self, msgs: list[ChatMsg]) -> None:
        for m in msgs:
            self.add(m)

    def clear(self) -> None:
        self._msgs.clear()
        self._save()

    def _trim(self) -> None:
        """Keep at most ``max_turns`` user turns (with their tool exchanges)."""
        user_idx = [i for i, m in enumerate(self._msgs) if m.get("role") == "user"]
        if len(user_idx) > self.max_turns:
            cut = user_idx[-self.max_turns]
            self._msgs = self._msgs[cut:]

    def close_interrupted(self, spoken: str) -> None:
        """The user cut Jarvis off. Record the reply as finished with only what was actually said, so the model does not
        resume it or answer the old question again alongside the new one."""
        spoken = (spoken or "").strip()
        note = f"{spoken} [interrupted]" if spoken else "[interrupted before I said anything]"
        if self._msgs and self._msgs[-1].get("role") == "assistant" and not self._msgs[-1].get("tool_calls"):
            self._msgs[-1] = {"role": "assistant", "content": note}
        elif self._msgs and self._msgs[-1].get("role") in ("user", "tool"):
            self.add({"role": "assistant", "content": note})

    def last_assistant_text(self) -> str:
        for m in reversed(self._msgs):
            if m.get("role") == "assistant" and m.get("content"):
                return str(m["content"])
        return ""
