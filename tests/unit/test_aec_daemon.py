"""The echo canceller inside the daemon: off by default, taps playback, and only unlocks interruption once it works."""

from __future__ import annotations

import numpy as np
from fakes import FakeSTT, FakeTTS, FakeWake
from test_aec import RATE, echo_of, room, speechlike

from jarvis_core.assistant import Assistant
from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.config import Config
from jarvis_core.daemon import Daemon, Hooks
from jarvis_core.frontend import FakeFrontend
from jarvis_core.voice.vad import EnergyVAD


def pcm(x) -> bytes:
    return np.clip(np.asarray(x), -32768, 32767).astype(np.int16).tobytes()  # clip: wrapping would add distortion


def make(enabled: bool, supports_aec: bool = False):
    cfg = Config()
    cfg.activation.aec = enabled
    cfg.activation.barge_in_speakers = False
    cfg.activation.space_to_interrupt = False  # this exercises the voice-interruption code, which is kept but off by default
    cfg.activation.duck_media = False  # never touch the real Spotify from a test
    fe = FakeFrontend()
    fe.supports_aec = supports_aec
    assistant = Assistant.create(cfg, llm=ScriptedLLM(["ok"]), text_only=True)
    d = Daemon(cfg, fe, assistant=assistant, stt=FakeSTT(), tts=FakeTTS(), wake=FakeWake(),
               vad_factory=EnergyVAD, hooks=Hooks())
    return d, fe


async def test_off_by_default_and_changes_nothing():
    d, fe = make(False)
    assert d._aec is None and not d._aec_trusted()
    frame = pcm(np.ones(320) * 700)
    d.on_audio(frame)  # no crash, and the frontend's methods are the originals
    assert fe.play_chunk.__name__ == "play_chunk"


async def test_a_frontend_with_its_own_echo_cancellation_does_not_need_ours():
    d, _ = make(True, supports_aec=True)
    assert d._aec is None


async def test_playback_is_tapped_and_stop_clears_it():
    d, fe = make(True)
    assert d._aec is not None and fe.play_chunk.__name__ == "tapped_chunk"
    await fe.play_chunk("u1", pcm(np.ones(4800) * 1000), 16000)
    assert d._aec_ref.playing and bytes(fe.played["u1"])  # still reached the speaker path
    await fe.stop_audio()
    assert not d._aec_ref.playing


async def test_interruption_on_speakers_is_trusted_only_once_the_canceller_is_working():
    d, fe = make(True)
    assert not d._voice_barge_ok()  # nothing learned yet: same caution as before
    ref = speechlike(8, level=2500)
    mic = echo_of(ref, room())
    for i in range(0, len(mic) - 320 + 1, 320):
        await fe.play_chunk("u", pcm(ref[i:i + 320]), RATE)
        await d._handle_frame(pcm(mic[i:i + 320]))
    assert d._aec.erle_db > d.cfg.activation.aec_min_erle_db
    assert d._voice_barge_ok()
    d.cfg.activation.barge_in = False
    assert not d._voice_barge_ok()  # the user's own switch still wins


async def test_frames_reaching_the_listener_are_the_cleaned_ones():
    d, fe = make(True)
    seen = []
    d.preroll.push = lambda f: seen.append(f)  # what the rest of the daemon is given
    ref = speechlike(6, level=2500)
    mic = echo_of(ref, room())
    for i in range(0, len(mic) - 320 + 1, 320):
        await fe.play_chunk("u", pcm(ref[i:i + 320]), RATE)
        await d._handle_frame(pcm(mic[i:i + 320]))
    tail_in = np.frombuffer(pcm(mic[-RATE:]), dtype=np.int16).astype(float)
    tail_out = np.frombuffer(b"".join(seen[-50:]), dtype=np.int16).astype(float)
    assert np.mean(tail_out**2) < 0.05 * np.mean(tail_in**2)
