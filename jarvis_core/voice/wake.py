"""Wake word detection (openWakeWord, pretrained ``hey_jarvis``)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

CHUNK_SAMPLES = 1280  # 80 ms at 16 kHz, openWakeWord's native frame


class WakeWordDetector(Protocol):
    def process(self, pcm16: bytes) -> bool:
        """Feed PCM16 mono 16 kHz audio; True when the wake word fires."""

    def reset(self) -> None: ...


class OpenWakeWordDetector:
    def __init__(
        self,
        wake_word: str = "hey_jarvis",
        threshold: float = 0.6,
        refractory_s: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from openwakeword.model import Model

        try:
            self._model = Model(wakeword_models=[wake_word], inference_framework="onnx")
        except Exception:
            # Models not downloaded yet: fetch the pretrained set once, then retry.
            import openwakeword

            openwakeword.utils.download_models()
            self._model = Model(wakeword_models=[wake_word], inference_framework="onnx")
        self.wake_word = wake_word
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._clock = clock
        self._buf = np.zeros(0, dtype=np.int16)
        self._last_fire = -1e9
        self.last_score = 0.0
        self.fired_score = 0.0  # the score that made it fire (the last chunk's score can be lower)

    def process(self, pcm16: bytes) -> bool:
        self._buf = np.concatenate([self._buf, np.frombuffer(pcm16, dtype=np.int16)])
        fired = False
        while len(self._buf) >= CHUNK_SAMPLES:
            chunk, self._buf = self._buf[:CHUNK_SAMPLES], self._buf[CHUNK_SAMPLES:]
            scores = self._model.predict(chunk)
            score = max((v for k, v in scores.items() if k.startswith(self.wake_word)), default=0.0)
            self.last_score = float(score)
            now = self._clock()
            if score >= self.threshold and now - self._last_fire >= self.refractory_s:
                self._last_fire = now
                fired = True
                self.fired_score = float(score)
        return fired

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        try:
            self._model.reset()
        except Exception:
            pass


class RefractoryGate:
    """Wraps a score stream with threshold + refractory logic (used by tests/other detectors)."""

    def __init__(self, threshold: float, refractory_s: float, clock: Callable[[], float] = time.monotonic):
        self.threshold = threshold
        self.refractory_s = refractory_s
        self._clock = clock
        self._last = -1e9

    def check(self, score: float) -> bool:
        now = self._clock()
        if score >= self.threshold and now - self._last >= self.refractory_s:
            self._last = now
            return True
        return False


def build_wake(wake_word: str, threshold: float) -> WakeWordDetector | None:
    try:
        return OpenWakeWordDetector(wake_word, threshold)
    except Exception as e:
        log.warning("wake word unavailable (%s); hotkey only", e)
        return None
