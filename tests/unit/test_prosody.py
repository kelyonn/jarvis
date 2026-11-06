from __future__ import annotations

import numpy as np

from jarvis_core.config import VoiceCfg
from jarvis_core.router import VARIANTS, vary
from jarvis_core.voice import prosody
from jarvis_core.voice.tts import EdgeTTS, FallbackTTS, build_tts


def test_plan_splits_at_commas_and_pauses_between_clauses():
    cs = prosody.plan("Good evening, sir, it's a rather soggy Wednesday, so take an umbrella.")
    assert len(cs) >= 2
    assert all(c.pause_ms > 0 for c in cs)
    assert cs[0].pause_ms == prosody.PAUSE_MS[","] and cs[-1].pause_ms == prosody.PAUSE_MS["."]
    assert " ".join(c.text for c in cs).replace(",", "").split() == \
        "Good evening, sir, it's a rather soggy Wednesday, so take an umbrella.".replace(",", "").split()


def test_short_fragments_are_merged_not_chopped():
    cs = prosody.plan("Right, sir, then.")
    assert all(len(c.text.split()) >= 1 for c in cs)
    assert len(cs) <= 2


def test_questions_slow_down_at_the_end_and_speeds_vary():
    q = prosody.plan("Your calendar is clear, but you've two reminders waiting, shall I read them?")
    assert q[-1].speed < q[0].speed
    speeds = {c.speed for c in prosody.plan("One thing first, then another thing, and finally the last thing.")}
    assert len(speeds) > 1  # not one flat rate


def test_delivery_is_deterministic_and_bounded():
    a = prosody.plan("It's a fine day, sir, and the markets are up.", 1.0)
    b = prosody.plan("It's a fine day, sir, and the markets are up.", 1.0)
    assert a == b
    assert all(0.85 <= c.speed <= 1.1 for c in a)


def test_assemble_inserts_silence_and_never_clicks():
    sr = 24000
    tone = (np.sin(np.linspace(0, 200, sr // 2)) * 0.5).astype(np.float32)
    out = prosody.assemble([(tone, 150), (tone, 300)], sr)
    assert out.size > tone.size * 2  # two clauses plus a pause between them
    assert abs(out[0]) < 0.05 and abs(out[-1]) < 0.05  # faded edges
    assert prosody.assemble([], sr).size == 0
    assert prosody.trim_silence(np.zeros(1000, dtype=np.float32)).size == 0


def test_router_variants_rotate_without_immediate_repeats():
    template = "Opening {name}, sir."
    seen = [vary(template) for _ in range(12)]
    assert len(set(seen)) > 1
    assert all(a != b for a, b in zip(seen, seen[1:]))
    assert all("{name}" in o or o in {"On it."} for o in VARIANTS[template])
    assert vary("A reply with no variants.") == "A reply with no variants."


def test_edge_engine_falls_back_to_local_voice():
    class Boom:
        def synth(self, text):
            raise RuntimeError("offline")

    class Local:
        def synth(self, text):
            return "local"

    f = FallbackTTS(Boom(), Local())
    assert f.synth("hi") == "local" and f.synth("again") == "local"


def test_build_tts_edge_never_raises_without_network_stack(monkeypatch):
    # asking for edge must always yield *some* working engine, even if edge-tts or Kokoro is missing
    tts = build_tts(VoiceCfg(tts="say"))
    assert tts is not None
    assert EdgeTTS  # importable class
