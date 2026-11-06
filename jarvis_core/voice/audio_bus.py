"""Pre-roll ring buffer of recent mic frames."""

from __future__ import annotations

from collections import deque

FRAME_MS = 20
FRAME_BYTES = 640  # 20 ms * 16 kHz * 2 bytes


class PreRoll:
    def __init__(self, ms: int = 1000) -> None:
        self._frames: deque[bytes] = deque(maxlen=max(1, ms // FRAME_MS))

    def push(self, frame: bytes) -> None:
        self._frames.append(frame)

    def tail(self, ms: int) -> bytes:
        n = max(1, ms // FRAME_MS)
        return b"".join(list(self._frames)[-n:])

    def clear(self) -> None:
        self._frames.clear()
