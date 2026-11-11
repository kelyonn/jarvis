"""Hearing you properly: wait for the rest of a sentence you paused in, and check before acting on a shaky transcript."""

from __future__ import annotations

import pytest
from fakes import SILENCE, Rig

from jarvis_core.interfaces import Transcript
from jarvis_core.state import State
from jarvis_core.voice.turn import looks_unfinished, needs_clarification

# ------------------------------------------------------------------ the phrase rules


@pytest.mark.parametrize("text", [
    "open the", "set a timer for", "call mom and", "what time is it, and", "remind me to", "play some music, um", "turn it up,",
])
def test_pauses_mid_sentence_are_recognised(text):
    assert looks_unfinished(text)


@pytest.mark.parametrize("text", [
    "open the calendar", "turn the light on", "what time is it", "stop", "the", "thanks.", "is it raining?", "do that", "",
])
def test_finished_or_tiny_utterances_are_not(text):
    assert not looks_unfinished(text)


def test_clarification_needs_real_doubt_and_a_real_request():
    assert needs_clarification(-1.6, "tell me something about whales", -1.25)
    assert not needs_clarification(-0.4, "tell me something about whales", -1.25)
    assert not needs_clarification(None, "tell me something about whales", -1.25)
    assert not needs_clarification(-2.0, "yes", -1.25)  # a single word is answered, not queried


# ------------------------------------------------------------------ waiting for the rest


class ScriptedSTT:
    def __init__(self, items):
        self.items, self.audio_lengths = list(items), []

    def transcribe(self, pcm16, sample_rate=16000):
        self.audio_lengths.append(len(pcm16))
        text, logprob = self.items.pop(0) if self.items else ("hello", None)
        return Transcript(text=text, duration_s=len(pcm16) / 32000, avg_logprob=logprob)


async def rig_with(items, **kw):
    rig = Rig(**kw)
    rig.stt = ScriptedSTT(items)
    rig.daemon.stt = rig.stt
    return await rig.start()


async def test_a_pause_mid_sentence_waits_and_hears_it_as_one_request():
    rig = await rig_with([("open the", None), ("open the calendar", None)], replies=["Opening it, sir."])
    try:
        await rig.daemon._begin_listening("followup", followup=True)
        await rig.say_utterance()  # "open the" ... then you stop
        await rig.wait_state(State.FOLLOW_UP, State.LISTENING)
        assert rig.assistant.history.messages() == []  # nothing was acted on yet
        await rig.say_utterance()  # "calendar"
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.assistant.history.messages()[0]["content"] == "open the calendar"
        first, second = rig.stt.audio_lengths
        assert second > first  # the second hearing included the first half
    finally:
        await rig.stop()


async def test_if_nothing_more_comes_the_first_half_is_used_after_a_short_wait():
    rig = await rig_with([("tell me a story about", None)], replies=["For how long, sir?"])
    try:
        await rig.daemon._begin_listening("followup", followup=True)
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.LISTENING)
        await rig.feed(SILENCE, int(rig.cfg.activation.extend_wait_s * 50) + 40)  # it waits, hears nothing, then goes on
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.assistant.history.messages()[0]["content"] == "tell me a story about"
    finally:
        await rig.stop()


async def test_it_only_waits_once_per_request():
    rig = await rig_with([("open the", None), ("open the and", None)], replies=["Hm, sir."])
    try:
        await rig.daemon._begin_listening("followup", followup=True)
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.LISTENING)
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.assistant.history.messages() == []  # waited once, then let the half-sentence go instead of answering it
    finally:
        await rig.stop()


async def test_the_wait_can_be_switched_off():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.extend_unfinished = False
    cfg.activation.wake_gate_rms = 0
    rig = await rig_with([("open the", None)], cfg=cfg, replies=["Which one, sir?"])
    try:
        await rig.daemon._begin_listening("followup", followup=True)
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.assistant.history.messages()[0]["content"] == "open the"
    finally:
        await rig.stop()


async def test_push_to_talk_is_never_extended():
    rig = await rig_with([("open the", None)], replies=["Which one, sir?"])
    try:
        await rig.daemon._begin_listening("hotkey")
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.assistant.history.messages()[0]["content"] == "open the"
    finally:
        await rig.stop()


# ------------------------------------------------------------------ checking before acting


async def test_a_shaky_transcript_is_checked_and_a_yes_goes_ahead():
    rig = await rig_with([("tell me something about whales", -1.8), ("yes", -0.2)], replies=["Whales are remarkable, sir."])
    try:
        await rig.daemon._begin_listening("hotkey")
        await rig.say_utterance()
        await rig.wait_state(State.CONFIRMING, timeout=5)
        assert "I think you said, tell me something about whales" in " ".join(rig.tts.spoken) and rig.tts.spoken[-1] == "Is that right?"
        assert rig.assistant.history.messages() == []  # not acted on yet
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.assistant.history.messages()[0]["content"] == "tell me something about whales"
    finally:
        await rig.stop()


async def test_a_no_drops_the_request_and_asks_again():
    rig = await rig_with([("tell me something about whales", -1.8), ("no", -0.2)], replies=["Never reached"])
    try:
        await rig.daemon._begin_listening("hotkey")
        await rig.say_utterance()
        await rig.wait_state(State.CONFIRMING, timeout=5)
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.assistant.history.messages() == []
        assert "Say it again" in rig.tts.spoken[-1]
    finally:
        await rig.stop()


async def test_a_confident_transcript_is_not_questioned():
    rig = await rig_with([("how do whales sleep", -0.3)], replies=["Noon, sir."])
    try:
        await rig.daemon._begin_listening("hotkey")
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.tts.spoken == ["Noon, sir."]
    finally:
        await rig.stop()


async def test_the_check_can_be_switched_off():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.voice.low_confidence_logprob = 0
    cfg.activation.wake_gate_rms = 0
    rig = await rig_with([("tell me something about whales", -3.0)], cfg=cfg, replies=["Whales are remarkable, sir."])
    try:
        await rig.daemon._begin_listening("hotkey")
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert rig.tts.spoken == ["Whales are remarkable, sir."]
    finally:
        await rig.stop()


async def test_stop_is_never_questioned_even_when_unclear():
    rig = await rig_with([("stop", -3.0)], replies=["Never reached"])
    try:
        await rig.daemon._begin_listening("hotkey")
        await rig.say_utterance()
        await rig.wait_state(State.IDLE)
        assert rig.tts.spoken == []
    finally:
        await rig.stop()
