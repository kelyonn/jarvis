from __future__ import annotations

import pytest

from jarvis_core.brain.sentence_splitter import SentenceSplitter
from jarvis_core.interfaces import Transcript
from jarvis_core.state import InvalidTransition, State, StateMachine
from jarvis_core.voice.earcons import make_earcon
from jarvis_core.voice.stt import filter_transcript
from jarvis_core.voice.text_norm import num_to_words, ordinal_words, speakable, spoken_time
from jarvis_core.voice.turn import (
    is_sleep_phrase,
    is_stop_phrase,
    parse_confirmation,
    strip_wake_phrase,
)
from jarvis_core.voice.vad import BargeInDetector, EndEvent, Endpointer, EndpointerParams, EnergyVAD
from jarvis_core.voice.wake import RefractoryGate

# --- sentence splitter -------------------------------------------------------


def _split(chunks, **kw):
    sp = SentenceSplitter(**kw)
    out = []
    for c in chunks:
        out += sp.feed(c)
    tail = sp.flush()
    if tail:
        out.append(tail)
    return out


def test_splitter_basic_streaming():
    out = _split(["Good morning, sir. It is ", "a fine day. Shall I ", "open the calendar?"])
    assert out == ["Good morning, sir.", "It is a fine day.", "Shall I open the calendar?"]


def test_splitter_waits_for_whitespace_after_period():
    sp = SentenceSplitter()
    assert sp.feed("The temperature is 22.") == []  # could be "22.5"
    assert sp.feed("5 degrees today. Lovely") == ["The temperature is 22.5 degrees today."]
    assert sp.flush() == "Lovely"


def test_splitter_abbreviations_and_short_fragments():
    out = _split(["Dr. Sharma called. He ", "will ring back at five."])
    assert out[0].startswith("Dr. Sharma called.")
    out = _split(["Yes. Certainly, sir. I shall do that right away."])
    assert out == ["Yes. Certainly, sir.", "I shall do that right away."]


def test_splitter_flush_and_long_run_on():
    long = "word " * 80
    out = _split([long, "end."])
    assert all(len(s) <= 260 for s in out) and out[-1].endswith("end.")


def test_splitter_question_marks_split_early():
    assert _split(["Really sir? I shall check."])[0] == "Really sir?"


# --- text normalisation -----------------------------------------------------


def test_num_to_words():
    assert num_to_words(0) == "zero"
    assert num_to_words(21) == "twenty-one"
    assert num_to_words(1_25_000, indian=True) == "one lakh twenty-five thousand"
    assert num_to_words(2_50_00_000, indian=True) == "two crore fifty lakh"
    assert num_to_words(1_250_000) == "one million two hundred fifty thousand"
    assert num_to_words(-5) == "minus five"


def test_ordinals():
    assert [ordinal_words(n) for n in (1, 2, 3, 12, 20, 21)] == [
        "first", "second", "third", "twelfth", "twentieth", "twenty-first"]


@pytest.mark.parametrize(
    "src,expected",
    [
        ("₹1,25,000", "one lakh twenty-five thousand rupees"),
        ("^NSEI is up", "the Nifty is up"),
        ("at 14:30", "at half past two"),
        ("meet at 9:00", "meet at nine o'clock"),
        ("10:45 am", "quarter to eleven in the morning"),
        ("22°C", "twenty-two degrees"),
        ("Tuesday the 3rd", "Tuesday the third"),
        ("see https://example.com/x now", "see now"),
        ("**bold** and `code`", "bold and code"),
        ("up 1.5%", "up one point five percent"),
        ("$12.50", "twelve dollars and fifty cents"),
        ("BTC-USD", "Bitcoin"),
        ("hello 😀", "hello"),
        ("in 2026", "in twenty twenty-six"),
    ],
)
def test_speakable(src, expected):
    assert speakable(src) == expected


def test_spoken_time():
    assert spoken_time(0, 0) == "midnight"
    assert spoken_time(13, 5) == "one oh five"
    assert spoken_time(22, 42) == "ten forty-two"


# --- vad / endpointing -------------------------------------------------------


def _run(ep: Endpointer, pattern: list[tuple[float, int]]) -> list[EndEvent]:
    out = []
    for prob, n in pattern:
        for _ in range(n):
            ev = ep.feed(prob)
            if ev != EndEvent.NONE:
                out.append(ev)
    return out


def test_endpointer_speech_then_silence_ends():
    ep = Endpointer(EndpointerParams(end_silence_ms=600))
    evs = _run(ep, [(0.0, 5), (0.9, 20), (0.0, 30)])
    assert evs == [EndEvent.SPEECH_START, EndEvent.SPEECH_END]


def test_endpointer_ignores_blips_and_times_out():
    ep = Endpointer(EndpointerParams(no_speech_timeout_s=1.0))
    evs = _run(ep, [(0.9, 1), (0.0, 40)])  # one 32ms blip then silence
    assert evs == [EndEvent.NO_SPEECH_TIMEOUT]


def test_endpointer_mid_phrase_pause_does_not_end():
    ep = Endpointer(EndpointerParams(end_silence_ms=600))
    evs = _run(ep, [(0.9, 10), (0.0, 10), (0.9, 10), (0.0, 25)])  # 320ms pause < 600ms
    assert evs == [EndEvent.SPEECH_START, EndEvent.SPEECH_END]
    assert ep.speech_ms >= 600


def test_endpointer_max_length():
    ep = Endpointer(EndpointerParams(max_s=1.0))
    evs = _run(ep, [(0.9, 60)])
    assert EndEvent.MAX_LENGTH in evs


def test_barge_in_requires_sustained_speech():
    b = BargeInDetector(threshold=0.75, sustain_ms=250)
    assert not any(b.feed(0.9) for _ in range(7))  # 224ms
    assert b.feed(0.9)  # 256ms
    b.reset()
    seq = [b.feed(p) for p in [0.9, 0.0, 0.9, 0.0, 0.9, 0.0]]  # choppy noise
    assert not any(seq)


def test_energy_vad_windows():
    vad = EnergyVAD()
    loud = (b"\x00\x0b" * 320)  # amplitude 2816, 20ms
    quiet = b"\x00\x00" * 320
    probs = vad.feed(loud * 4)  # 80ms -> two 32ms windows (+remainder)
    assert len(probs) == 2 and all(p > 0.9 for p in probs)
    vad.reset()
    assert all(p == 0 for p in vad.feed(quiet * 4))


def test_refractory_gate():
    t = [0.0]
    g = RefractoryGate(0.6, 1.5, clock=lambda: t[0])
    assert g.check(0.9)
    t[0] = 1.0
    assert not g.check(0.9)
    t[0] = 2.0
    assert g.check(0.9)
    assert not g.check(0.2)


# --- phrases -----------------------------------------------------------------


@pytest.mark.parametrize(
    "src,out",
    [
        ("Hey Jarvis, what time is it", "what time is it"),
        ("Jarvis open Spotify", "open Spotify"),
        ("hey jarvis", ""),
        ("open Spotify", "open Spotify"),
    ],
)
def test_strip_wake_phrase(src, out):
    assert strip_wake_phrase(src) == out


def test_stop_and_sleep_phrases():
    assert is_stop_phrase("Stop.") and is_stop_phrase("Jarvis, never mind")
    assert not is_stop_phrase("stop the music")
    assert is_sleep_phrase("go to sleep")


@pytest.mark.parametrize(
    "src,val",
    [
        ("yes", True), ("Yes, do it", True), ("go ahead", True), ("sure", True),
        ("no", False), ("no don't do it", False), ("cancel", False), ("not now", False),
        ("hmm maybe", None), ("", None), (None, None),
    ],
)
def test_parse_confirmation(src, val):
    assert parse_confirmation(src) is val


# --- stt filter / earcons / state machine ------------------------------------


def test_filter_transcript():
    assert filter_transcript(Transcript("Thank you."), 2.0) == ""
    assert filter_transcript(Transcript("open spotify"), 0.2) == ""
    assert filter_transcript(Transcript("open spotify"), 1.5) == "open spotify"
    assert filter_transcript(Transcript("the the the the the the the"), 2.0) == ""
    assert filter_transcript(Transcript("hmm", no_speech_prob=0.95, avg_logprob=-1.5), 2.0) == ""


def test_earcons_nonempty_and_distinct():
    a, b = make_earcon("listen"), make_earcon("error")
    assert len(a) > 1000 and a != b and len(a) % 2 == 0


def test_state_machine_strict():
    sm = StateMachine(strict=True)
    sm.go(State.IDLE)
    sm.go(State.LISTENING)
    with pytest.raises(InvalidTransition):
        sm.go(State.SPEAKING)
    sm.go(State.PAUSED)
    with pytest.raises(InvalidTransition):
        sm.go(State.IDLE)  # must go through RESUMING
    sm.go(State.RESUMING)
    sm.go(State.IDLE)


def test_repetition_loops_are_noise_but_real_speech_is_not():
    from jarvis_core.voice.stt import has_repetition_loop

    assert has_repetition_loop("I'm not sure what I'm doing. " * 6)
    assert has_repetition_loop("WhatsApp, iMessage, WhatsApp, iMessage, WhatsApp, iMessage, WhatsApp, iMessage")
    assert not has_repetition_loop("No no no I want the other one, the other one please")
    assert not has_repetition_loop("open spotify and play my radiohead playlist")
    assert not has_repetition_loop("")


def test_pronunciation_fixes():
    from jarvis_core.voice.text_norm import speakable

    assert "the the" not in speakable("the S&P 500 is up")
    assert speakable("after 4pm") == "after four o'clock in the afternoon"
    assert speakable("ran 5km and used 20GB") == "ran five kilometres and used twenty gigabytes"
    assert speakable("due 2026-11-04") == "due November fourth, twenty twenty-six"
    assert "Bengaloo-roo" in speakable("weather in Bengaluru") and "eye Message" in speakable("open iMessage")


def test_list_markers_in_replies():
    from jarvis_core.voice.text_norm import caption_text, speakable

    t = "Right, two things: - Battery's at 15 percent. - Ethereum's up 6.9 percent. Done, sir."
    assert "dash" not in speakable(t) and " - " not in speakable(t)
    shown = caption_text(t)
    assert shown.count("\n• ") == 2 and shown.startswith("Right, two things:")


def test_mentions_wake_word_tolerates_misrecognition_but_not_chatter():
    from jarvis_core.voice.turn import mentions_wake_word

    for ok in ["Hey Jarvis what's up", "jarvis", "hey Jarvus play music", "Jarvas, stop"]:
        assert mentions_wake_word(ok), ok
    for bad in ["I'm going to go to the next video", "and apps are", "stomp", "I'm in so much trouble"]:
        assert not mentions_wake_word(bad), bad


def test_sleep_phrases_with_and_without_wake_word():
    from jarvis_core.voice.turn import is_sleep_phrase

    for ok in ("stop listening", "Jarvis, stop listening", "hey jarvis go to sleep", "stop listening please",
               "sleep", "Jarvis sleep mode"):
        assert is_sleep_phrase(ok), ok
    for bad in ("what's the weather", "I can't sleep well", "stop"):
        assert not is_sleep_phrase(bad), bad


def test_wake_word_must_come_first():
    from jarvis_core.voice.turn import starts_with_wake_word

    for ok in ("hey jarvis", "Hey Jarvis, what's up", "hey jarvis what time is it"):
        assert starts_with_wake_word(ok), ok
    for bad in ("Jarvis", "jarvis what time is it", "what time is it jarvis", "hi there", ""):
        assert not starts_with_wake_word(bad), bad


def test_wake_phrase_is_found_after_a_short_lead_in_and_stripped():
    from jarvis_core.voice.turn import starts_with_wake_word, strip_wake_phrase

    assert starts_with_wake_word("so um hey Jarvis set a timer for 25 seconds")
    assert strip_wake_phrase("so um hey Jarvis, set a timer for 25 seconds") == "set a timer for 25 seconds"
    assert strip_wake_phrase("Hi Jarvis what time is it") == "what time is it"
    assert not starts_with_wake_word("set a timer for 25 seconds")
    assert not starts_with_wake_word("I was telling my friend yesterday that jarvis is a funny name")
