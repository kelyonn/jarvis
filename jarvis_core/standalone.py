"""Standalone frontend: Python mic + speaker via ``sounddevice``. No UI, no echo cancellation.

While Jarvis speaks, mic frames are suppressed (no barge-in) so it cannot trigger on itself.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

import numpy as np

from jarvis_core.frontend import Frontend
from jarvis_core.voice.earcons import make_earcon

log = logging.getLogger(__name__)


class StandaloneFrontend(Frontend):
    supports_aec = False
    has_ui = False

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in_stream = None
        self._speaking = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._mic_enabled = True

    async def start(self) -> None:
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()

        def cb(indata, frames, time_info, status):  # runs on the PortAudio thread
            if self._speaking.is_set() or not self._mic_enabled:
                return
            data = bytes(indata)
            if self.events and self._loop:
                self._loop.call_soon_threadsafe(self.events.on_audio, data)

        self._in_stream = sd.RawInputStream(
            samplerate=16000, channels=1, dtype="int16", blocksize=320, callback=cb
        )
        self._in_stream.start()
        self._worker = threading.Thread(target=self._play_worker, daemon=True, name="jarvis-playback")
        self._worker.start()
        if self.events:
            self.events.on_connected({"standalone": True})

    async def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        if self._in_stream:
            self._in_stream.stop()
            self._in_stream.close()
            self._in_stream = None

    async def set_mic_enabled(self, enabled: bool) -> None:
        self._mic_enabled = enabled

    # -- playback -----------------------------------------------------------
    def _play_worker(self) -> None:
        import sounddevice as sd

        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            kind, utt, payload = item
            if kind == "chunk":
                pcm, sr = payload
                self._speaking.set()
                arr = np.frombuffer(pcm, dtype=np.int16)
                try:
                    sd.play(arr, sr)
                    sd.wait()
                except Exception:
                    log.exception("playback failed")
            elif kind == "end":
                self._speaking.clear()
                self._resolve(utt, "finished")

    def _resolve(self, utt: str, event: str) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self.resolve_playback, utt, event)

    async def play_begin(self, utt: str, interruptible: bool = True) -> None:
        self.expect_playback(utt)

    async def play_chunk(self, utt: str, pcm16: bytes, sample_rate: int) -> None:
        self._q.put(("chunk", utt, (pcm16, sample_rate)))

    async def play_end(self, utt: str) -> None:
        self._q.put(("end", utt, None))

    async def stop_audio(self) -> None:
        import sounddevice as sd

        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        sd.stop()
        self._speaking.clear()
        await super().stop_audio()

    async def earcon(self, name: str) -> None:
        import sounddevice as sd

        if not self._speaking.is_set():
            sd.play(np.frombuffer(make_earcon(name), dtype=np.int16), 24000)
