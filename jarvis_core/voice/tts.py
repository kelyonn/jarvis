"""Text-to-speech: Kokoro (``bm_george``), with macOS ``say`` as the fallback."""

from __future__ import annotations

import logging
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from jarvis_core.config import VoiceCfg
from jarvis_core.interfaces import TTS, AudioChunk
from jarvis_core.voice import prosody

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000


class SayTTS:
    """macOS ``say`` rendered to a 24 kHz WAV."""

    def __init__(self, voice: str = "Daniel", rate_wpm: int = 185) -> None:
        self.voice = voice
        self.rate = rate_wpm

    def synth(self, text: str) -> AudioChunk:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "s.wav"
            subprocess.run(
                ["say", "-v", self.voice, "-r", str(self.rate), "-o", str(out),
                 "--file-format=WAVE", f"--data-format=LEI16@{SAMPLE_RATE}", text],
                check=True, capture_output=True, timeout=60,
            )
            with wave.open(str(out), "rb") as wf:
                return AudioChunk(wf.readframes(wf.getnframes()), wf.getframerate())


class KokoroTTS:
    """Kokoro via the ``kokoro`` package (British male voices ``bm_*``).

    ``voice`` may be several voices joined by commas (e.g. ``bm_george,bm_fable``): Kokoro averages their style
    vectors, which gives a less synthetic timbre than any single preset. With ``humanize`` on, each sentence is
    rendered clause by clause with varied pacing and natural pauses (see ``prosody``).
    """

    def __init__(self, voice: str = "bm_george", speed: float = 1.0, humanize: bool = True) -> None:
        from kokoro import KPipeline

        self.speed = speed
        self.humanize = humanize
        lang = "b" if voice[:1] == "b" else "a"
        self._pipe = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
        self.voice = self._load_voice(voice)

    def _load_voice(self, voice: str) -> str:
        """Use the requested (possibly blended) voice; if a part can't be fetched, fall back to the first one."""
        try:
            self._pipe.load_voice(voice)
            return voice
        except Exception as e:  # noqa: BLE001 - offline, or an unknown voice name
            first = voice.split(",")[0].strip()
            log.warning("Kokoro voice %r unavailable (%s); using %s", voice, e, first)
            return first

    def _render(self, text: str, speed: float) -> np.ndarray:
        parts: list[np.ndarray] = []
        for _, _, audio in self._pipe(text, voice=self.voice, speed=speed):
            if audio is None:
                continue
            arr = audio.cpu().numpy() if hasattr(audio, "cpu") else np.asarray(audio)
            parts.append(arr.astype(np.float32))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def synth(self, text: str) -> AudioChunk:
        if self.humanize:
            clauses = prosody.plan(text, self.speed)
            pieces = [(self._render(c.text, c.speed), c.pause_ms) for c in clauses]
            pcm = prosody.assemble(pieces, SAMPLE_RATE)
        else:
            pcm = self._render(text, self.speed)
        if pcm.size == 0:
            return AudioChunk(b"", SAMPLE_RATE)
        pcm = np.clip(pcm, -1.0, 1.0)
        return AudioChunk((pcm * 32767).astype(np.int16).tobytes(), SAMPLE_RATE)

    def warm_up(self) -> None:
        self.synth("Ready.")


class EdgeTTS:
    """Microsoft's neural voices through the free ``edge-tts`` package: far more expressive than a local model.

    Needs the internet, and the text Jarvis speaks is sent to Microsoft's speech service, so it is opt-in
    (``tts = "edge"``). Anything that fails drops through to the local voice via ``FallbackTTS``.
    """

    def __init__(self, voice: str = "en-GB-RyanNeural", rate: str = "+0%", pitch: str = "+0Hz") -> None:
        import edge_tts  # noqa: F401 - fail early if the package is missing

        self.voice, self.rate, self.pitch = voice, rate, pitch

    async def _fetch(self, text: str, path: Path) -> None:
        import edge_tts

        await edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch).save(str(path))

    def synth(self, text: str) -> AudioChunk:
        import asyncio

        with tempfile.TemporaryDirectory() as d:
            mp3, wav = Path(d) / "s.mp3", Path(d) / "s.wav"
            asyncio.run(asyncio.wait_for(self._fetch(text, mp3), 20))
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", f"LEI16@{SAMPLE_RATE}", "-c", "1", str(mp3), str(wav)],
                check=True, capture_output=True, timeout=30,
            )
            with wave.open(str(wav), "rb") as wf:
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        pcm = prosody.assemble([(pcm, 200)], SAMPLE_RATE)
        return AudioChunk((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes(), SAMPLE_RATE)


class FallbackTTS:
    """Tries ``primary``; on failure permanently degrades to ``fallback``."""

    def __init__(self, primary: TTS, fallback: TTS) -> None:
        self.primary = primary
        self.fallback = fallback
        self._failed = False

    def synth(self, text: str) -> AudioChunk:
        if not self._failed:
            try:
                return self.primary.synth(text)
            except Exception as e:
                log.error("primary TTS failed (%s); switching to fallback", e)
                self._failed = True
        return self.fallback.synth(text)


def _local(cfg: VoiceCfg, say: TTS) -> TTS:
    try:
        return FallbackTTS(KokoroTTS(cfg.tts_voice, cfg.tts_speed, cfg.humanize), say)
    except Exception as e:
        log.warning("Kokoro unavailable (%s); using macOS say", e)
    return say


def build_tts(cfg: VoiceCfg) -> TTS:
    say = SayTTS(cfg.say_voice)
    if cfg.tts == "say":
        return say
    local = _local(cfg, say)
    if cfg.tts == "edge":
        try:
            return FallbackTTS(EdgeTTS(cfg.edge_voice, cfg.edge_rate, cfg.edge_pitch), local)
        except Exception as e:
            log.warning("edge-tts unavailable (%s); using the local voice", e)
    return local
