"""Speech-to-text: mlx-whisper (primary), faster-whisper via the bridge (fallback)."""

from __future__ import annotations

import logging
import re

import numpy as np

from jarvis_core.config import VoiceCfg
from jarvis_core.interfaces import STT, Transcript

log = logging.getLogger(__name__)

_HALLUCINATIONS = {
    "thank you", "thank you.", "thanks for watching", "thanks for watching!", "you", "bye", ".",
    "thank you for watching", "thank you for watching!", "subtitles by the amara.org community",
    "okay", "uh", "um", "hmm", "mm-hmm",
}


def build_prompt(extra_terms: list[str] | None = None) -> str:
    terms = ["Jarvis", "WhatsApp", "iMessage", "Gmail", "LinkedIn", "Spotify", "Nifty", "Sensex"]
    terms += extra_terms or []
    return "Hey Jarvis. Names and apps: " + ", ".join(dict.fromkeys(terms)) + "."


def filter_transcript(t: Transcript, audio_s: float) -> str:
    """Drop junk transcripts; return cleaned text or ''."""
    text = t.text.strip()
    if audio_s < 0.4 or not text:
        return ""
    low = re.sub(r"\s+", " ", text.lower()).strip()
    if low in _HALLUCINATIONS:
        return ""
    if t.no_speech_prob is not None and t.no_speech_prob > 0.8 and (t.avg_logprob or 0) < -1.0:
        return ""
    # Repetition loops ("the the the the ...")
    words = low.split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return ""
    return text


class MlxWhisperSTT:
    def __init__(self, model: str, prompt: str | None = None) -> None:
        import mlx_whisper  # noqa: F401

        self.model = model
        self.prompt = prompt or build_prompt()
        self._warm = False

    def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> Transcript:
        import mlx_whisper

        audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        res = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language="en",
            initial_prompt=self.prompt,
            condition_on_previous_text=False,
            temperature=0.0,
            verbose=None,
        )
        segs = res.get("segments") or []
        avg_lp = float(np.mean([s.get("avg_logprob", 0.0) for s in segs])) if segs else None
        nsp = float(np.max([s.get("no_speech_prob", 0.0) for s in segs])) if segs else None
        return Transcript(
            text=(res.get("text") or "").strip(),
            duration_s=len(audio) / sample_rate,
            avg_logprob=avg_lp,
            no_speech_prob=nsp,
        )

    def warm_up(self) -> None:
        if not self._warm:
            self.transcribe(np.zeros(16000, dtype=np.int16).tobytes())
            self._warm = True


def build_stt(cfg: VoiceCfg, extra_terms: list[str] | None = None) -> STT:
    prompt = build_prompt(extra_terms)
    if cfg.stt == "mlx-whisper":
        try:
            return MlxWhisperSTT(cfg.stt_model, prompt)
        except Exception as e:
            log.warning("mlx-whisper unavailable (%s); trying faster-whisper", e)
    from jarvis_core.adapters.services import FasterWhisperSTT

    return FasterWhisperSTT("small")


def has_repetition_loop(text: str, min_repeats: int = 3) -> bool:
    """Speech recognisers stuck on echo or silence repeat one phrase ("I'm not sure what I'm doing" x20).
    People do not say the same words three times running, so such a transcript is noise, not a request."""
    words = re.sub(r"[^a-z0-9' ]", " ", text.lower()).split()
    n = len(words)
    for size in range(1, 13):
        for start in range(0, n - size * min_repeats + 1):
            gram = words[start:start + size]
            reps = 1
            pos = start + size
            while words[pos:pos + size] == gram:
                reps += 1
                pos += size
            if reps >= min_repeats and size * reps >= max(6, n * 0.5):
                return True
    return False
