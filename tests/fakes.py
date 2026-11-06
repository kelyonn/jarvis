"""Shared test doubles for the voice daemon."""

from __future__ import annotations

import asyncio
import time

from jarvis_core.assistant import Assistant
from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.config import Config
from jarvis_core.daemon import Daemon, Hooks
from jarvis_core.frontend import FakeFrontend
from jarvis_core.interfaces import AudioChunk, Transcript
from jarvis_core.skills.base import CmdResult
from jarvis_core.state import State
from jarvis_core.voice.vad import EnergyVAD

SPEECH = b"\x00\x0b" * 320  # 20 ms, amplitude ~2816 -> speech for EnergyVAD
SILENCE = b"\x00\x00" * 320


class FakeWake:
    def __init__(self) -> None:
        self.armed = False
        self.resets = 0

    def process(self, pcm16: bytes) -> bool:
        if self.armed:
            self.armed = False
            return True
        return False

    def reset(self) -> None:
        self.resets += 1


class FakeSTT:
    def __init__(self, texts: list[str] | None = None) -> None:
        self.texts = list(texts or [])
        self.calls = 0

    def transcribe(self, pcm16: bytes, sample_rate: int = 16000) -> Transcript:
        self.calls += 1
        text = self.texts.pop(0) if self.texts else "hello"
        return Transcript(text=text, duration_s=len(pcm16) / 32000)


class FakeTTS:
    def __init__(self) -> None:
        self.spoken: list[str] = []

    def synth(self, text: str) -> AudioChunk:
        self.spoken.append(text)
        return AudioChunk(b"\x01\x00" * 2400, 24000)


class Rig:
    """A daemon wired with fakes plus helpers to drive it."""

    def __init__(
        self,
        *,
        replies: list | None = None,
        stt_texts: list[str] | None = None,
        auto_finish: bool = True,
        cfg: Config | None = None,
        assistant=None,
    ) -> None:
        self.cfg = cfg or Config()
        self.cfg.activation.duck_media = False  # never touch the real Spotify from a test
        if cfg is None:
            self.cfg.activation.wake_gate_rms = 0  # tests feed silence frames and expect the wake word to see them
        self.frontend = FakeFrontend(auto_finish=auto_finish)
        self.llm = ScriptedLLM(replies or ["Good evening, sir."])
        self.assistant = assistant or Assistant.create(self.cfg, llm=self.llm, text_only=True)
        self.wake = FakeWake()
        self.stt = FakeSTT(stt_texts)
        self.tts = FakeTTS()
        self.hooks = Hooks()
        self.daemon = Daemon(
            self.cfg, self.frontend, assistant=self.assistant, stt=self.stt, tts=self.tts,
            wake=self.wake, vad_factory=EnergyVAD, hooks=self.hooks, strict_states=False,
        )
        self.task: asyncio.Task | None = None

    async def start(self) -> Rig:
        self.task = asyncio.create_task(self.daemon.run())
        await self.wait_state(State.IDLE)
        return self

    async def stop(self) -> None:
        self.daemon.request_stop()
        if self.task:
            await asyncio.wait_for(self.task, 5)

    async def wait_state(self, *states: State, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while self.daemon.state not in states:
            if time.monotonic() > end:
                raise AssertionError(
                    f"timeout waiting for {[s.value for s in states]}, in {self.daemon.state.value}"
                )
            await asyncio.sleep(0.005)

    async def feed(self, frame: bytes, n: int) -> None:
        for i in range(n):
            self.daemon.on_audio(frame)
            if i % 5 == 4:
                await asyncio.sleep(0)
        await asyncio.sleep(0.01)

    async def say_utterance(self, speech_frames: int = 30, silence_frames: int = 45) -> None:
        await self.feed(SPEECH, speech_frames)
        await self.feed(SILENCE, silence_frames)

    async def wake_and_speak(self) -> None:
        self.wake.armed = True
        await self.feed(SILENCE, 2)
        await self.wait_state(State.LISTENING)
        await self.say_utterance()


class FakeRunner:
    """Records subprocess calls and answers from a prefix table; nothing touches the real system."""

    def __init__(self, responses: dict[tuple[str, ...], CmdResult] | None = None) -> None:
        self._CmdResult = CmdResult
        self.responses = responses or {}
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    async def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.kwargs.append(kw)
        best = None
        for prefix, res in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best[0])):
                best = (prefix, res)
        return best[1] if best else self._CmdResult(0, "")

    def ran(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)
