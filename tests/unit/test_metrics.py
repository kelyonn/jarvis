"""Timings kept locally: written safely, summarised plainly, recorded by a real voice turn."""

from __future__ import annotations

import json

import pytest
from fakes import SILENCE, SPEECH, Rig

from jarvis_core import cli
from jarvis_core.metrics import Metrics, _pct, format_summary
from jarvis_core.state import State


def test_percentiles():
    vals = [10, 20, 30, 40, 50]
    assert _pct(vals, 50) == 30 and _pct(vals, 0) == 10 and _pct(vals, 100) == 50
    assert _pct([7], 95) == 7 and _pct([], 50) == 0
    assert abs(_pct(vals, 95) - 48) < 1e-9


def test_records_and_summarises(tmp_path):
    m = Metrics(tmp_path / "m.jsonl", clock=lambda: 1000.0)
    for v in (100, 200, 300, 400):
        m.record("stt_ms", v, audio_s=1.5)
    m.record("cpu_pct", 8)
    s = m.summary()
    assert s["stt_ms"]["n"] == 4 and s["stt_ms"]["p50"] == 250 and s["stt_ms"]["max"] == 400
    text = format_summary(s)
    assert "stt_ms" in text and "median" in text and text.index("stt_ms") < text.index("cpu_pct") or "cpu_pct" in text


def test_no_words_are_ever_stored(tmp_path):
    m = Metrics(tmp_path / "m.jsonl")
    m.record("stt_ms", 120, audio_s=2.0)
    row = json.loads((tmp_path / "m.jsonl").read_text())
    assert set(row) == {"t", "stage", "v", "audio_s"}
    assert oct((tmp_path / "m.jsonl").stat().st_mode & 0o777) == "0o600"


def test_a_broken_file_does_not_stop_anything(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"stage": "stt_ms", "v": 5, "t": 1}\nnot json\n{"nope": 1}\n')
    m = Metrics(p, clock=lambda: 10.0)
    assert m.summary()["stt_ms"]["n"] == 1
    Metrics(tmp_path / "missing" / "m.jsonl").record("stt_ms", 1)  # unwritable folder: silently ignored


def test_old_measurements_can_be_left_out(tmp_path):
    now = [100 * 86400.0]
    m = Metrics(tmp_path / "m.jsonl", clock=lambda: now[0])
    m.record("stt_ms", 900)
    now[0] += 5 * 86400
    m.record("stt_ms", 100)
    assert m.summary(days=2)["stt_ms"]["n"] == 1 and m.summary()["stt_ms"]["n"] == 2


def test_the_file_rotates_instead_of_growing_forever(tmp_path):
    from jarvis_core import metrics

    p = tmp_path / "m.jsonl"
    p.write_text("x" * (metrics.MAX_BYTES + 1))
    Metrics(p).record("stt_ms", 1)
    assert p.with_suffix(".old").exists() and p.stat().st_size < 200


def test_empty_summary_says_so():
    assert "No measurements yet" in format_summary({})


def test_stats_command_prints(tmp_path, capsys, monkeypatch):
    Metrics().record("to_first_audio_ms", 850)
    with pytest.raises(SystemExit) as e:
        cli.main(["stats"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    assert "to_first_audio_ms" in out and "850" in out


async def test_a_voice_turn_records_the_delay_you_feel():
    rig = await Rig(stt_texts=["what time is it"], replies=["It is noon, sir."]).start()
    try:
        d = rig.daemon
        await d._begin_listening("hotkey")
        for _ in range(30):
            await d._handle_frame(SPEECH)
        for _ in range(60):
            await d._handle_frame(SILENCE)
        await rig.wait_state(State.FOLLOW_UP, State.IDLE, timeout=5)
        stages = {r["stage"]: r["v"] for r in d.metrics.read()}
        assert {"stt_ms", "think_ms", "to_first_audio_ms", "turn_ms"} <= set(stages)
        assert stages["to_first_audio_ms"] >= stages["think_ms"] >= 0
    finally:
        await rig.stop()
