"""Voice activity detection and utterance endpointing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512  # 32 ms at 16 kHz (Silero's required window)
WINDOW_MS = 32.0


class VAD(Protocol):
    def feed(self, pcm16: bytes) -> list[float]:
        """Consume PCM16 mono 16 kHz; return one speech probability per 32 ms window."""

    def reset(self) -> None: ...


class _Windower:
    def __init__(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)

    def push(self, pcm16: bytes) -> list[np.ndarray]:
        self._buf = np.concatenate([self._buf, np.frombuffer(pcm16, dtype=np.int16)])
        out = []
        while len(self._buf) >= WINDOW_SAMPLES:
            out.append(self._buf[:WINDOW_SAMPLES])
            self._buf = self._buf[WINDOW_SAMPLES:]
        return out

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)


class EnergyVAD:
    """Fallback VAD: maps RMS energy to a pseudo-probability."""

    def __init__(self, threshold_rms: float = 450.0) -> None:
        self._w = _Windower()
        self.threshold = threshold_rms

    def feed(self, pcm16: bytes) -> list[float]:
        probs = []
        for w in self._w.push(pcm16):
            rms = float(np.sqrt(np.mean(w.astype(np.float32) ** 2)))
            probs.append(min(1.0, rms / (2 * self.threshold)))  # threshold -> 0.5
        return probs

    def reset(self) -> None:
        self._w.reset()


class SileroVAD:
    """Silero VAD (torch). Raises ImportError if ``silero-vad`` is unavailable."""

    def __init__(self) -> None:
        import torch
        from silero_vad import load_silero_vad

        self._torch = torch
        self._model = load_silero_vad()
        self._w = _Windower()

    def feed(self, pcm16: bytes) -> list[float]:
        probs = []
        torch = self._torch
        for w in self._w.push(pcm16):
            x = torch.from_numpy(w.astype(np.float32) / 32768.0)
            with torch.no_grad():
                probs.append(float(self._model(x, SAMPLE_RATE).item()))
        return probs

    def reset(self) -> None:
        self._w.reset()
        self._model.reset_states()


def build_vad() -> VAD:
    try:
        return SileroVAD()
    except Exception as e:
        log.warning("Silero VAD unavailable (%s); falling back to energy VAD", e)
        return EnergyVAD()


class EndEvent(str, Enum):
    NONE = "none"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"
    NO_SPEECH_TIMEOUT = "no_speech_timeout"
    MAX_LENGTH = "max_length"


@dataclass
class EndpointerParams:
    speech_threshold: float = 0.5
    end_silence_ms: float = 600.0
    no_speech_timeout_s: float = 5.0
    max_s: float = 30.0
    min_speech_ms: float = 96.0  # ignore blips shorter than 3 windows


class Endpointer:
    """Finds the end of an utterance from a stream of speech probabilities."""

    def __init__(self, params: EndpointerParams) -> None:
        self.p = params
        self.elapsed_ms = 0.0
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.started = False
        self.done = False

    def feed(self, prob: float, dt_ms: float = WINDOW_MS) -> EndEvent:
        if self.done:
            return EndEvent.NONE
        self.elapsed_ms += dt_ms
        is_speech = prob >= self.p.speech_threshold
        if is_speech:
            self.speech_ms += dt_ms
            self.silence_ms = 0.0
            if not self.started and self.speech_ms >= self.p.min_speech_ms:
                self.started = True
                return EndEvent.SPEECH_START
        else:
            if self.started:
                self.silence_ms += dt_ms
            else:
                self.speech_ms = 0.0  # blip ended before reaching min length
        if self.started and self.silence_ms >= self.p.end_silence_ms:
            self.done = True
            return EndEvent.SPEECH_END
        if not self.started and self.elapsed_ms >= self.p.no_speech_timeout_s * 1000:
            self.done = True
            return EndEvent.NO_SPEECH_TIMEOUT
        if self.elapsed_ms >= self.p.max_s * 1000:
            self.done = True
            return EndEvent.MAX_LENGTH
        return EndEvent.NONE


class BargeInDetector:
    """Detects the user talking over Jarvis (sustained speech on the AEC'd mic)."""

    def __init__(self, threshold: float = 0.75, sustain_ms: float = 250.0) -> None:
        self.threshold = threshold
        self.sustain_ms = sustain_ms
        self._run = 0.0

    def feed(self, prob: float, dt_ms: float = WINDOW_MS) -> bool:
        if prob >= self.threshold:
            self._run += dt_ms
        else:
            self._run = max(0.0, self._run - dt_ms * 2)
        return self._run >= self.sustain_ms

    def reset(self) -> None:
        self._run = 0.0
