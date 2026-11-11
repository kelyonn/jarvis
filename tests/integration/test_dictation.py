"""Hold a key, speak, let go: cleaned text is typed into the front app, safely."""

from __future__ import annotations

import pytest
from fakes import SILENCE, SPEECH, Rig

from jarvis_core.config import Config
from jarvis_core.state import State
from jarvis_core.voice.dictation import clean_dictation

# ------------------------------------------------------------------ cleaning


@pytest.mark.parametrize("said,typed", [
    ("um so I think we should uh meet on friday", "So I think we should meet on friday"),
    ("the the meeting is at three", "The meeting is at three"),
    ("hello comma how are you question mark", "Hello, how are you?"),
    ("thanks exclamation mark see you soon full stop", "Thanks! See you soon."),
    ("buy milk new line buy eggs", "Buy milk buy eggs"),
    ("  lots   of   space  ", "Lots of space"),
    ("", ""),
    ("um uh", ""),
])
def test_cleaning(said, typed):
    assert clean_dictation(said) == typed


def test_spoken_punctuation_can_be_left_alone():
    assert clean_dictation("a period of time", spoken_punctuation=False) == "A period of time"


def test_output_is_always_one_line():
    assert "\n" not in clean_dictation("one\ntwo\r\nthree new paragraph four")


# ------------------------------------------------------------------ the key


def front(app="Notes"):
    def handler(op, args):
        return {"app": app} if op == "ax.frontmost" else {"ok": True}

    return handler


async def hold_and_speak(rig, *, speak=40):
    d = rig.daemon
    d.on_hotkey("dictate_down")
    await rig.wait_state(State.LISTENING)
    await rig.feed(SPEECH, speak)
    await rig.feed(SILENCE, 200)  # a long pause while still holding: must not end it
    assert d.state == State.LISTENING
    d.on_hotkey("dictate_up")
    await rig.wait_state(State.IDLE)


def typed(rig):
    return [a["text"] for op, a in rig.frontend.requests if op == "input.type"]


async def test_dictation_types_the_cleaned_words_and_says_nothing():
    rig = Rig(stt_texts=["um hello comma world"])
    rig.frontend.request_handler = front()
    await rig.start()
    try:
        await hold_and_speak(rig)
        assert typed(rig) == ["Hello, world"]
        assert rig.tts.spoken == [] and rig.assistant.history.messages() == []  # no assistant, no reply
    finally:
        await rig.stop()


async def test_a_quick_tap_types_nothing():
    rig = Rig(stt_texts=["should never be used"])
    rig.frontend.request_handler = front()
    await rig.start()
    try:
        rig.daemon.on_hotkey("dictate_down")
        await rig.wait_state(State.LISTENING)
        rig.daemon.on_hotkey("dictate_up")
        await rig.wait_state(State.IDLE)
        assert typed(rig) == [] and rig.stt.calls == 0
    finally:
        await rig.stop()


async def test_apps_set_to_never_are_not_typed_into():
    rig = Rig(stt_texts=["my secret"])
    rig.frontend.request_handler = front("1Password")
    await rig.start()
    try:
        await hold_and_speak(rig)
        assert typed(rig) == [] and any("1Password" in c for c in rig.frontend.captions)
    finally:
        await rig.stop()


async def test_the_user_can_allow_a_normally_blocked_app():
    cfg = Config()
    cfg.activation.wake_gate_rms = 0
    cfg.safety.app_rules = {"terminal": "auto"}
    rig = Rig(cfg=cfg, stt_texts=["ls minus la"])
    rig.frontend.request_handler = front("Terminal")
    await rig.start()
    try:
        await hold_and_speak(rig)
        assert typed(rig) == ["Ls minus la"]
    finally:
        await rig.stop()


async def test_a_failure_to_type_is_reported_not_hidden():
    def handler(op, args):
        if op == "ax.frontmost":
            return {"app": "Notes"}
        raise RuntimeError("refusing to type into a password field")

    rig = Rig(stt_texts=["hello there"])
    rig.frontend.request_handler = handler
    await rig.start()
    try:
        await hold_and_speak(rig)
        assert any("couldn't type" in c for c in rig.frontend.captions) and "error" in rig.frontend.earcons
        assert rig.daemon.state == State.IDLE
    finally:
        await rig.stop()


async def test_dictation_can_be_switched_off():
    cfg = Config()
    cfg.activation.dictation = False
    cfg.activation.wake_gate_rms = 0
    rig = Rig(cfg=cfg)
    await rig.start()
    try:
        rig.daemon.on_hotkey("dictate_down")
        await rig.feed(SILENCE, 5)
        assert rig.daemon.state == State.IDLE
    finally:
        await rig.stop()


async def test_the_key_does_nothing_while_jarvis_is_busy():
    rig = Rig(auto_finish=False, replies=["A long answer, sir."], stt_texts=["tell me about whales"])
    await rig.start()
    try:
        await rig.daemon._begin_listening("hotkey")
        await rig.say_utterance()
        await rig.wait_state(State.SPEAKING, State.THINKING)
        rig.daemon.on_hotkey("dictate_down")
        await rig.feed(SILENCE, 5)
        assert rig.daemon.state in (State.SPEAKING, State.THINKING)
    finally:
        await rig.stop()
