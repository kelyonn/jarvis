"""The echo canceller against simulated rooms: it must remove the echo, keep your voice, and never invent a signal."""

from __future__ import annotations

import numpy as np
import pytest

from jarvis_core.voice import aec as A

B = A.FRAME
RATE = A.RATE


def speechlike(seconds: float, seed: int = 1, level: float = 6000.0) -> np.ndarray:
    """Band-limited noise with a syllable-rate envelope: crude, but it excites the filter the way speech does."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    x = rng.standard_normal(n)
    kernel = np.hanning(9)
    x = np.convolve(x, kernel / kernel.sum(), mode="same")
    t = np.arange(n) / RATE
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 3.5 * t) ** 2
    return (x * env * level / x.std()).astype(np.float32)


def room(delay_ms: float = 80, tail_ms: float = 150, gain: float = 0.5, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    d, n = int(delay_ms * RATE / 1000), int(tail_ms * RATE / 1000)
    h = np.zeros(d + n, dtype=np.float32)
    h[d:] = rng.standard_normal(n) * np.exp(-np.arange(n) / (n / 4))
    return (h * gain / np.abs(h).max()).astype(np.float32)


def run(ref: np.ndarray, mic: np.ndarray, canceller: A.EchoCanceller | None = None) -> tuple[np.ndarray, A.EchoCanceller]:
    c = canceller or A.EchoCanceller()
    out = np.zeros_like(mic)
    for i in range(0, len(mic) - B + 1, B):
        out[i:i + B] = c.process(mic[i:i + B], ref[i:i + B])
    return out, c


def db(x: np.ndarray) -> float:
    return 10 * np.log10(float(np.mean(x.astype(np.float64) ** 2)) + 1e-9)


def echo_of(ref: np.ndarray, h: np.ndarray) -> np.ndarray:
    return np.convolve(ref, h)[: len(ref)].astype(np.float32)


# ------------------------------------------------------------------ cancelling


@pytest.mark.parametrize("delay_ms,gain", [(40, 0.5), (120, 0.3), (250, 0.7)])
def test_echo_is_removed_after_a_second_or_two(delay_ms, gain):
    ref = speechlike(10)
    mic = echo_of(ref, room(delay_ms=delay_ms, gain=gain))
    out, c = run(ref, mic)
    last = slice(6 * RATE, 10 * RATE)  # judged after it has had time to learn
    assert db(mic[last]) - db(out[last]) > 20
    assert c.erle_db > 15


def test_a_noisy_room_still_cancels():
    ref = speechlike(10)
    rng = np.random.default_rng(3)
    mic = echo_of(ref, room()) + rng.standard_normal(len(ref)).astype(np.float32) * 30
    out, _ = run(ref, mic)
    last = slice(6 * RATE, 10 * RATE)
    assert db(mic[last]) - db(out[last]) > 12


def test_your_voice_survives_while_it_cancels_the_echo():
    ref = speechlike(12, seed=1)
    voice = np.zeros_like(ref)
    seg = slice(8 * RATE, 10 * RATE)
    voice[seg] = speechlike(2, seed=99, level=5000)
    mic = echo_of(ref, room()) + voice
    out, c = run(ref, mic)
    kept = float(np.dot(out[seg], voice[seg]) / np.dot(voice[seg], voice[seg]))
    assert 0.7 < kept < 1.3  # the part of the output that is your voice has about the right size
    assert db(out[seg] - voice[seg]) < db(voice[seg]) - 8  # and what is left over is much quieter than you


def test_it_keeps_cancelling_after_you_talk_over_it():
    ref = speechlike(14, seed=2)
    voice = np.zeros_like(ref)
    voice[6 * RATE:8 * RATE] = speechlike(2, seed=11, level=7000)
    mic = echo_of(ref, room()) + voice
    out, _ = run(ref, mic)
    after = slice(11 * RATE, 14 * RATE)
    assert db(mic[after]) - db(out[after]) > 15  # talking over it did not wreck what it had learned


def test_silence_in_means_audio_through_unchanged():
    mic = speechlike(1, seed=5)
    out, _ = run(np.zeros_like(mic), mic)
    assert np.array_equal(out, mic[: len(out)])


def test_it_does_not_invent_a_signal_from_silence():
    out, c = run(speechlike(3), np.zeros(3 * RATE, dtype=np.float32))
    assert float(np.abs(out).max()) < 5 and c.erle_db < 1


def test_it_resets_cleanly_after_playback_stops():
    ref = np.concatenate([speechlike(4), np.zeros(3 * RATE, dtype=np.float32), speechlike(4, seed=4)])
    mic = echo_of(ref, room())
    out, c = run(ref, mic)
    assert db(mic[-3 * RATE:]) - db(out[-3 * RATE:]) > 15  # learns again when playback restarts


def test_wrong_sized_blocks_are_rejected():
    with pytest.raises(ValueError):
        A.EchoCanceller().process(np.zeros(10, dtype=np.float32), np.zeros(10, dtype=np.float32))


# ------------------------------------------------------------------ the reference timeline


def pcm(x: np.ndarray) -> bytes:
    return x.astype(np.int16).tobytes()


def test_timeline_plays_back_at_the_microphone_rate_with_silence_when_empty():
    t = A.ReferenceTimeline()
    assert not t.playing and not t.next(B).any()
    t.push(pcm(np.arange(1, 801)), RATE)  # 800 samples queued at once, as the Shell receives them
    assert t.playing
    assert t.next(B)[0] == 1 and t.next(B)[0] == 321
    last = t.next(B)  # 160 samples left, then silence
    assert last[0] == 641 and last[159] == 800 and not last[160:].any() and not t.playing


def test_timeline_resamples_tts_audio_to_16k():
    t = A.ReferenceTimeline()
    t.push(pcm(np.ones(2400) * 1000), 24000)  # 0.1 s at 24 kHz
    assert t._n == 1600


def test_stop_empties_the_timeline():
    t = A.ReferenceTimeline()
    t.push(pcm(np.ones(8000)), RATE)
    t.stop()
    assert not t.playing and not t.next(B).any()


def test_a_runaway_queue_is_capped():
    t = A.ReferenceTimeline(max_seconds=1)
    for _ in range(5):
        t.push(pcm(np.ones(RATE)), RATE)
    assert t._n <= RATE


def test_process_frame_passes_other_sizes_through():
    c, t = A.EchoCanceller(), A.ReferenceTimeline()
    assert A.process_frame(c, t, b"\x01\x00" * 100) == b"\x01\x00" * 100
    out = A.process_frame(c, t, pcm(np.ones(B) * 500))
    assert len(out) == B * 2
