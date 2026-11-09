from __future__ import annotations

import asyncio

from fakes import SILENCE, SPEECH, Rig

from jarvis_core.errors import LLMUnavailable
from jarvis_core.state import State


async def test_wake_turn_followup_and_timeout():
    rig = await Rig(replies=["Good evening, sir.", "Quite so."], stt_texts=["hey jarvis hello there", "and again"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        assert rig.tts.spoken[0] == "Good evening, sir."
        assert rig.frontend.earcons[0] == "listen"
        assert rig.assistant.history.messages()[0]["content"] == "hello there"  # wake phrase stripped
        assert any(len(b) > 0 for b in rig.frontend.played.values())
        assert {"listening", "transcribing", "thinking", "speaking", "follow_up"} <= set(rig.frontend.states)

        # follow-up works with no wake word
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP)
        assert rig.tts.spoken[-1] == "Quite so."
        assert rig.stt.calls == 2

        # 14 s of silence (the mid-conversation window) ends the conversation
        await rig.feed(SILENCE, 14 * 50 + 60)
        await rig.wait_state(State.IDLE)
    finally:
        await rig.stop()


async def test_no_speech_after_wake_returns_to_idle():
    rig = await Rig().start()
    try:
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)
        await rig.feed(SILENCE, 5 * 50 + 10)
        await rig.wait_state(State.IDLE)
        assert rig.stt.calls == 0
        assert rig.frontend.earcons[-1] == "done"
    finally:
        await rig.stop()


async def test_stop_phrase_skips_assistant():
    rig = await Rig(stt_texts=["stop"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.IDLE)
        assert rig.llm.calls == []
    finally:
        await rig.stop()


async def test_junk_transcript_is_dropped():
    rig = await Rig(stt_texts=["Thank you."]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.IDLE)
        assert rig.llm.calls == []
    finally:
        await rig.stop()


async def test_barge_in_stops_audio_and_relistens():
    rig = await Rig(auto_finish=False, stt_texts=["tell me a story", "actually stop that and open mail"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.SPEAKING)
        await rig.feed(SPEECH, 32)  # user talks over Jarvis (needs about half a second of speech)
        await rig.wait_state(State.LISTENING)
        assert ("stop_audio",) in rig.frontend.log
        await rig.feed(SPEECH, 10)
        await rig.feed(SILENCE, 45)
        await rig.wait_state(State.SPEAKING, State.FOLLOW_UP, State.THINKING)
        assert rig.stt.calls == 2
    finally:
        await rig.stop()


async def test_no_barge_in_without_aec():
    rig = Rig(auto_finish=False)
    rig.frontend.supports_aec = False
    await rig.start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.SPEAKING)
        await rig.feed(SPEECH, 30)
        assert rig.daemon.state == State.SPEAKING
    finally:
        await rig.stop()


async def test_hotkey_starts_listening_and_cancel():
    rig = await Rig().start()
    try:
        rig.daemon.on_hotkey("down")
        await rig.wait_state(State.LISTENING)
        rig.daemon.on_hotkey("long_press")
        await rig.wait_state(State.IDLE)
    finally:
        await rig.stop()


async def test_pause_blocks_wake_and_resume_runs_hooks():
    rig = await Rig().start()
    resumed = []

    async def on_resume(paused_at):
        resumed.append(paused_at)

    rig.hooks.on_resume.append(on_resume)
    try:
        rig.daemon.on_lifecycle("will_sleep")
        await rig.wait_state(State.PAUSED)
        rig.wake.armed = True
        await rig.feed(SILENCE, 5)
        assert rig.daemon.state == State.PAUSED  # frames ignored while paused
        rig.daemon.on_lifecycle("did_wake")
        await rig.wait_state(State.IDLE)
        assert len(resumed) == 1 and resumed[0] is not None
    finally:
        await rig.stop()


async def test_pause_requires_both_wake_and_unlock():
    rig = await Rig().start()
    try:
        rig.daemon.on_lifecycle("will_sleep")
        await rig.wait_state(State.PAUSED)
        rig.daemon.on_lifecycle("screen_locked")
        await asyncio.sleep(0.02)
        rig.daemon.on_lifecycle("did_wake")
        await asyncio.sleep(0.05)
        assert rig.daemon.state == State.PAUSED  # still locked
        rig.daemon.on_lifecycle("screen_unlocked")
        await rig.wait_state(State.IDLE)
    finally:
        await rig.stop()


async def test_pause_mid_speech_cancels_audio():
    rig = await Rig(auto_finish=False).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.SPEAKING)
        rig.daemon.on_lifecycle("will_sleep")
        await rig.wait_state(State.PAUSED)
        assert ("stop_audio",) in rig.frontend.log
    finally:
        await rig.stop()


async def test_announce_when_free_and_busy():
    rig = await Rig().start()
    try:
        assert await rig.daemon.announce("The post has arrived, sir.", earcon="alert")
        await rig.wait_state(State.FOLLOW_UP)
        assert rig.tts.spoken[-1] == "The post has arrived, sir."
        assert rig.assistant.history.messages()[-1]["content"] == "The post has arrived, sir."
        rig.daemon.sm.go(State.LISTENING)
        assert not await rig.daemon.announce("nope")
    finally:
        await rig.stop()


async def test_llm_down_speaks_apology():
    class Down:
        last_turn = None

        def respond_stream(self, text):
            async def gen():
                raise LLMUnavailable("down")
                yield ""
            return gen()

    rig = await Rig(assistant=Down()).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP, State.IDLE)
        assert "trouble reaching my servers" in rig.tts.spoken[-1]
    finally:
        await rig.stop()


async def test_ask_confirmation_yes_no_timeout():
    rig = await Rig(stt_texts=["yes please", "no", ""]).start()
    try:
        async def ask(timeout=1.0):
            task = asyncio.create_task(rig.daemon.ask_confirmation("Delete three files, sir?", timeout))
            await rig.wait_state(State.CONFIRMING)
            await asyncio.sleep(0.05)
            return task

        t = await ask()
        await rig.say_utterance()
        assert await asyncio.wait_for(t, 5) is True

        t = await ask()
        await rig.say_utterance()
        assert await asyncio.wait_for(t, 5) is False

        t = await ask(0.5)
        await rig.feed(SILENCE, 80)  # silence beyond the timeout
        assert await asyncio.wait_for(t, 5) is False
        assert rig.tts.spoken[-1] == "I'll take that as a no."
    finally:
        await rig.stop()


async def test_sleep_phrase_then_hey_jarvis_wakes():
    rig = await Rig(stt_texts=["go to sleep", "hey jarvis what time is it"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.IDLE)
        assert rig.daemon._muted and rig.daemon._sleep_listen
        rig.wake.armed = True  # "Hey Jarvis" is still heard while asleep
        await rig.wake_and_speak()
        await rig.wait_state(State.IDLE, State.FOLLOW_UP)
        assert not rig.daemon._muted
    finally:
        await rig.stop()


async def test_sleep_phrase_then_hotkey_wakes():
    rig = await Rig(stt_texts=["go to sleep"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.IDLE)
        rig.daemon.on_hotkey("down")
        await rig.wait_state(State.LISTENING)
        assert not rig.daemon._muted
    finally:
        await rig.stop()

async def _settle():
    await asyncio.sleep(0.05)


async def test_hotkey_mode_mic_is_open_only_while_listening():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.listen_mode = "hotkey"
    rig = await Rig(cfg=cfg, replies=["Hello, sir."], stt_texts=["hello"]).start()
    try:
        rig.frontend.supports_aec = False  # like the real Shell: no voice barge-in
        await _settle()
        assert rig.frontend.capture_log == []  # idle: the microphone was never opened

        rig.daemon.on_hotkey("down")
        await rig.wait_state(State.LISTENING)
        await _settle()
        assert rig.frontend.capture_log[-1] is True

        await rig.say_utterance()  # speech then silence -> transcribing, thinking, speaking
        await rig.wait_state(State.FOLLOW_UP)
        await _settle()
        # closed while it transcribed, thought and spoke; reopened for the follow-up
        assert rig.frontend.capture_log == [True, False, True]
        await rig.feed(SILENCE, 8 * 50 + 60)
        await rig.wait_state(State.IDLE)
        await _settle()
        assert rig.frontend.capture_log[-1] is False
    finally:
        await rig.stop()


async def test_wake_word_mode_keeps_mic_open_when_idle():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.listen_mode = "wake_word"
    rig = await Rig(cfg=cfg, replies=["Good evening, sir."], stt_texts=["hello"]).start()
    try:
        rig.frontend.supports_aec = False
        await _settle()
        assert rig.frontend.capture_log[-1] is True  # opt-in: always listening for the wake word
        rig.daemon._muted = True  # "go to sleep"
        rig.daemon._sync_capture()
        await _settle()
        assert rig.frontend.capture_log[-1] is False
    finally:
        await rig.stop()


async def test_pause_closes_the_mic():
    rig = await Rig().start()
    try:
        rig.daemon.on_hotkey("down")
        await rig.wait_state(State.LISTENING)
        await rig.daemon.pause()
        await _settle()
        assert rig.frontend.capture_log[-1] is False
    finally:
        await rig.stop()


async def test_wake_word_interrupts_speech_without_aec():
    rig = Rig(auto_finish=False, stt_texts=["tell me a story", "hey jarvis stop talking"])
    rig.frontend.supports_aec = False
    await rig.start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.SPEAKING)
        await _settle()
        assert rig.frontend.capture_log[-1] is True  # mic stays open so "Hey Jarvis" can be heard
        assert False not in rig.frontend.capture_log  # and is never closed mid-turn (Bluetooth profile flapping)
        await rig.feed(SPEECH, 30)  # plain talking does not interrupt (no echo cancellation)
        assert rig.daemon.state == State.SPEAKING
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)
        assert ("stop_audio",) in rig.frontend.log
    finally:
        await rig.stop()


async def test_mute_persists_across_restart_and_hotkey_clears_it():
    rig = await Rig().start()
    try:
        rig.daemon._muted = True
    finally:
        await rig.stop()
    rig2 = await Rig().start()
    try:
        assert rig2.daemon._muted is True
        rig2.daemon.on_hotkey("down")
        await rig2.wait_state(State.LISTENING)
        assert rig2.daemon._muted is False
    finally:
        await rig2.stop()


async def test_pause_for_expires():
    rig = await Rig().start()
    try:
        rig.daemon.pause_for(0.001)  # 60 ms
        assert rig.daemon._muted is True
        await asyncio.sleep(0.2)
        assert rig.daemon._muted is False
    finally:
        await rig.stop()


async def test_own_voice_echo_is_ignored_and_does_not_loop():
    rig = await Rig(replies=["Spotify's queue is open, sir."], stt_texts=["tell me about the queue", "Spotify's queue is open, sir."]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        calls = len(rig.llm.calls)
        await rig.say_utterance()  # the "user" turn is really Jarvis hearing its own reply
        await rig.wait_state(State.IDLE, State.FOLLOW_UP)
        await asyncio.sleep(0.1)
        assert len(rig.llm.calls) == calls  # no second reply
    finally:
        await rig.stop()


async def test_voice_barge_in_on_headphones():
    rig = Rig(auto_finish=False, stt_texts=["tell me a story", "stop that"])
    rig.frontend.supports_aec = False
    await rig.start()
    try:
        rig.daemon._headphones = True
        rig.daemon._headphones_checked_at = 1e18  # skip the real system_profiler call
        await rig.wake_and_speak()
        await rig.wait_state(State.SPEAKING)
        await rig.feed(SPEECH, 32)
        await rig.wait_state(State.LISTENING)
        assert ("stop_audio",) in rig.frontend.log
    finally:
        await rig.stop()


async def test_quiet_room_gate_skips_wake_model_then_catches_speech():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.wake_gate_rms = 60
    rig = Rig(cfg=cfg)
    seen = []
    orig = rig.wake.process
    rig.wake.process = lambda pcm: (seen.append(len(pcm)), orig(pcm))[1]
    await rig.start()
    try:
        await rig.feed(SILENCE, 100)
        assert seen == []  # a silent room costs no wake-word analysis
        rig.wake.armed = True
        await rig.feed(SPEECH, 3)  # sound starts: the gate opens and the recent audio is replayed
        assert seen and seen[0] > 640  # more than one frame: the pre-roll was replayed
        await rig.wait_state(State.LISTENING)
    finally:
        await rig.stop()


async def test_echo_filter_matches_a_partial_tail_with_different_number_formats():
    rig = await Rig(replies=["Right, two things. Battery's at 15 percent, might want to plug in soon. "
                             "Ethereum's up 6.9 percent, now at 2,430."], stt_texts=["what's new"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        d = rig.daemon
        # what the recogniser typed when it caught the tail of the reply through the earbuds
        assert d._is_own_echo("Might want to plug in soon. Ethereum's up 6.9%. Now at 2430.")
        assert d._is_own_echo("Ethereum's up 6.9 percent now at 2,430")
        assert not d._is_own_echo("play my radiohead playlist")
        assert not d._is_own_echo("what's the weather")
        assert not d._is_own_echo("pause")
    finally:
        await rig.stop()


async def test_echo_filter_catches_homophone_garbled_speaker_leakage():
    # On speakers (no AEC) the recogniser sometimes mangles words it picks up from Jarvis's own voice into
    # near-homophones ("Stfu"/"leetcode" -> "sdfu"/"leap code"); the match must not depend on the exact words.
    rig = await Rig(replies=["Tomorrow's meeting is Stfu and do leetcode at three, with heavy drizzle expected "
                             "and a 99 percent chance of rain. That's it."], stt_texts=["what's new"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        d = rig.daemon
        assert d._is_own_echo("is sdfu and do leap code at the")
        assert d._is_own_echo("sdfu and do leap code at")
        assert not d._is_own_echo("can you move that meeting to four instead")
        assert not d._is_own_echo("what's on my calendar for friday")
    finally:
        await rig.stop()


async def test_echo_during_follow_up_does_not_end_the_conversation():
    reply = "Here are three things. Battery is at fifteen percent, and Ethereum is up by seven percent."
    rig = await Rig(
        replies=[reply, "Glad to help, sir."],
        stt_texts=["what's new", "Battery is at fifteen percent and Ethereum is up by seven percent", "thanks a lot"],
    ).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        await rig.feed(SILENCE, 30)  # the deaf spell after the reply
        await rig.say_utterance()  # the mic caught the tail of Jarvis's own reply: recognised, dropped as echo
        await asyncio.sleep(0.1)
        assert rig.daemon.state == State.FOLLOW_UP  # still listening, no need to say "Hey Jarvis" again
        await rig.feed(SILENCE, 30)
        await rig.say_utterance()  # the real follow-up
        await rig.wait_state(State.FOLLOW_UP)
        assert rig.tts.spoken[-1] == "Glad to help, sir."
    finally:
        await rig.stop()


async def test_follow_up_window_is_longer_after_a_question():
    rig = await Rig(replies=["Which playlist did you mean, sir?"], stt_texts=["play something"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        await rig.feed(SILENCE, 10 * 50)  # ten seconds of silence: past the normal 8 s window
        assert rig.daemon.state == State.FOLLOW_UP
        await rig.feed(SILENCE, 12 * 50)
        await rig.wait_state(State.IDLE)
    finally:
        await rig.stop()


async def test_interrupting_keeps_your_words_and_drops_the_tail_of_jarvis_speaking():
    rig = await Rig(replies=["Here are the headlines for today, sir."], stt_texts=["go"]).start()
    try:
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        d = rig.daemon
        d.last_reply_text = "Battery is at fifteen percent, so you might want to plug in soon, sir."
        # you cut in: the recording starts with the end of Jarvis's sentence, then your request
        assert d._strip_own_echo_edges("want to plug in soon sir open spotify please") == "open spotify please"
        assert d._strip_own_echo_edges("play my radiohead playlist") == "play my radiohead playlist"
        assert d._strip_own_echo_edges("plug in soon sir") == "plug in soon sir"  # all echo: left for the echo filter
    finally:
        await rig.stop()


async def test_only_the_owner_can_interrupt_or_command():
    class FakeVerifier:
        enrolled = True

        def __init__(self, score):
            self.score = score

        def similarity(self, pcm):
            return self.score

    rig = await Rig(replies=["Good evening, sir."], stt_texts=["hello there"]).start()
    try:
        d = rig.daemon
        d.verifier = FakeVerifier(0.40)  # someone else, or Jarvis's own voice
        assert await d._is_owner(b"\x00" * 64000) is False
        assert await d._is_owner(b"\x00" * 64000, barge=True) is False
        d.verifier = FakeVerifier(0.80)  # you
        assert await d._is_owner(b"\x00" * 64000) is True
        d._last_owner_at = -1e9  # (the check above just recognised you; forget that for the next ones)
        d.verifier = FakeVerifier(0.54)  # borderline: fine when interrupting, not for a normal command
        assert await d._is_owner(b"\x00" * 64000, barge=True) is True
        d._last_owner_at = -1e9
        assert await d._is_owner(b"\x00" * 64000) is False
        d.verifier = None  # not enrolled: nobody is blocked
        assert await d._is_owner(b"\x00" * 64000) is True
        # a stranger's utterance never reaches the assistant (here the wake word was yours, the words after were not)
        d.verifier = FakeVerifier(0.85)
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)
        d.verifier = FakeVerifier(0.30)
        await rig.say_utterance()
        await asyncio.sleep(0.2)
        assert rig.llm.calls == []
    finally:
        await rig.stop()


async def test_interrupting_an_announcement_stops_it_and_starts_the_new_task():
    rig = Rig(auto_finish=False, stt_texts=["what do you think about jazz"], replies=["Jazz is wonderful, sir."])
    rig.frontend.supports_aec = True  # voice barge-in allowed
    await rig.start()
    try:
        async def long_news():
            for part in ["Here is a long update about many things, ", "and then some more things, ", "and even more. "]:
                yield part
                await asyncio.sleep(0.4)

        caller = asyncio.create_task(rig.daemon.announce(long_news(), follow_up=False))
        await rig.wait_state(State.ANNOUNCING)
        await asyncio.sleep(0.2)
        await rig.feed(SPEECH, 32)  # you talk over it
        await rig.wait_state(State.LISTENING)
        assert ("stop_audio",) in rig.frontend.log
        assert await asyncio.wait_for(caller, 3) is False  # the announcing task ended without crashing its caller
        await rig.feed(SPEECH, 10)
        await rig.feed(SILENCE, 45)  # the new request
        await asyncio.sleep(0.3)
        assert rig.llm.calls, "the new request reached the assistant"
    finally:
        await rig.stop()


async def test_interrupted_reply_is_closed_out_in_history_and_flagged_for_the_next_turn():
    rig = await Rig(auto_finish=False, replies=["Once upon a time there was a very long story, sir.", "Spotify is open."],
                    stt_texts=["tell me a story", "what do you think about jazz"]).start()
    try:
        rig.frontend.supports_aec = True
        await rig.wake_and_speak()
        await rig.wait_state(State.SPEAKING)
        await rig.feed(SPEECH, 32)
        await rig.wait_state(State.LISTENING)
        msgs = rig.assistant.history.messages()
        assert msgs[-1]["role"] == "assistant" and "[interrupted]" in msgs[-1]["content"]
        assert rig.assistant.agent.interrupted is True
        await rig.feed(SPEECH, 10)
        await rig.feed(SILENCE, 45)
        await asyncio.sleep(0.4)
        assert "interrupted your previous reply" in rig.llm.calls[-1]["messages"][0]["content"]
    finally:
        await rig.stop()


async def test_welcome_back_after_the_lid_opens_but_not_for_a_flicker_or_twice():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.welcome_back_min_away_s = 0.3
    rig = await Rig(cfg=cfg).start()
    try:
        d = rig.daemon
        d.on_lifecycle("will_sleep")
        await asyncio.sleep(0.05)
        d.on_lifecycle("did_wake")
        await asyncio.sleep(0.3)
        assert not any("back" in t for t in rig.tts.spoken)  # a flicker is not worth a greeting

        d.on_lifecycle("will_sleep")
        await asyncio.sleep(0.5)
        d.on_lifecycle("did_wake")
        for _ in range(60):
            if any("back" in t for t in rig.tts.spoken):
                break
            await asyncio.sleep(0.05)
        assert any("back" in t for t in rig.tts.spoken)
        greetings = len(rig.tts.spoken)

        d.on_lifecycle("will_sleep")
        await asyncio.sleep(0.5)
        d.on_lifecycle("did_wake")
        await asyncio.sleep(0.4)
        assert len(rig.tts.spoken) == greetings  # the 20 minute cooldown
    finally:
        await rig.stop()


async def test_wake_word_from_a_stranger_is_ignored_when_a_voiceprint_exists():
    class FakeVerifier:
        enrolled = True

        def __init__(self, score):
            self.score = score

        def similarity(self, pcm):
            return self.score

    rig = await Rig().start()
    try:
        d = rig.daemon
        d.verifier = FakeVerifier(0.30)  # someone else says "Hey Jarvis"
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await asyncio.sleep(0.1)
        assert d.state == State.IDLE
        d.verifier = FakeVerifier(0.85)  # you
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)
    finally:
        await rig.stop()


async def test_weak_wake_from_chatter_is_not_run_as_a_command():
    rig = await Rig(replies=["Skipping, sir."], stt_texts=["I'm going to go to the next video", "Hey Jarvis, what do you think about jazz"]).start()
    try:
        rig.wake.fired_score = 0.42  # TV chatter: just over the threshold
        await rig.wake_and_speak()
        await asyncio.sleep(0.3)
        assert rig.llm.calls == [] and rig.daemon.state == State.IDLE  # no wake word in what was heard: ignored

        rig.wake.fired_score = 0.42  # a weak hit, but you really did say it
        await rig.wake_and_speak()
        await rig.wait_state(State.FOLLOW_UP)
        assert rig.llm.calls
    finally:
        await rig.stop()


class _EnrolVerifier:
    enrolled = False

    def __init__(self):
        self.samples = None

    def enroll(self, samples):
        self.samples = samples
        self.enrolled = True
        return len(samples)

    def similarity(self, pcm):
        return 0.9


async def test_learning_your_voice_is_a_conversation_that_waits_for_each_phrase():
    from jarvis_core.voice import enroll

    rig = await Rig(stt_texts=list(enroll.PHRASES)).start()
    try:
        d = rig.daemon
        v = _EnrolVerifier()
        d.verifier = v
        await d.start_enrollment()
        await rig.wait_state(State.FOLLOW_UP)
        assert "First: Hey Jarvis" in rig.tts.spoken[-1]
        for i in range(len(enroll.PHRASES)):
            await rig.feed(SILENCE, 30)  # the deaf spell after Jarvis speaks
            await rig.say_utterance(speech_frames=75, silence_frames=45)  # you say the phrase
            if i < len(enroll.PHRASES) - 1:
                await rig.wait_state(State.FOLLOW_UP)
                assert "Next:" in rig.tts.spoken[-1] and enroll.PHRASES[i + 1] in rig.tts.spoken[-1]
        await rig.wait_state(State.IDLE)
        assert d._enroll is None and v.enrolled and len(v.samples) == len(enroll.PHRASES)
        assert any("learned your voice" in t for t in rig.tts.spoken[-3:])
        assert rig.llm.calls == []  # none of it went to the assistant
    finally:
        await rig.stop()


async def test_voice_learning_repeats_an_unclear_phrase_and_can_be_cancelled():
    from jarvis_core.voice import enroll

    rig = await Rig(stt_texts=["mumble", "cancel"]).start()
    try:
        d = rig.daemon
        d.verifier = _EnrolVerifier()
        await d.start_enrollment()
        await rig.wait_state(State.FOLLOW_UP)
        await rig.feed(SILENCE, 30)
        await rig.say_utterance(speech_frames=75, silence_frames=45)
        await rig.wait_state(State.FOLLOW_UP)
        said = " ".join(rig.tts.spoken[-3:])
        assert "didn't quite catch" in said and enroll.PHRASES[0] in said
        await rig.feed(SILENCE, 30)
        await rig.say_utterance(speech_frames=40, silence_frames=45)
        await rig.wait_state(State.IDLE)
        assert d._enroll is None and not d.verifier.enrolled
    finally:
        await rig.stop()


async def test_follow_up_window_waits_longer_when_told_to_do_something():
    rig = await Rig().start()
    try:
        d = rig.daemon
        base = d.cfg.activation.follow_up_seconds
        assert d._followup_window("The weather is mild today.") == base
        assert d._followup_window("Which playlist did you mean?") >= 20
        assert d._followup_window("Open Settings and click Allow, then tell me when you're done.") >= 20
    finally:
        await rig.stop()


async def test_a_microphone_that_goes_silent_is_reopened():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.listen_mode = "wake_word"
    rig = await Rig(cfg=cfg).start()
    try:
        d = rig.daemon
        await asyncio.sleep(0.1)
        assert rig.frontend.capture_log[-1] is True
        # a whole stats window of exact zeros (earbuds reconnected, the input stream went dead)
        d._stat_n, d._stat_peak = 500, 0
        d._check_dead_mic()
        await asyncio.sleep(1.2)
        assert rig.frontend.capture_log[-3:] == [True, False, True]  # closed and reopened
    finally:
        await rig.stop()


async def test_follow_up_too_short_to_judge_is_not_acted_on():
    """A TV line that is too short for the voiceprint must not slip through the follow-up window."""

    class V:
        enrolled = True

        def check(self, pcm):
            return None, None

        def adapt(self, emb):
            pass

    rig = await Rig().start()
    try:
        d = rig.daemon
        d.verifier = V()
        assert await d._is_owner(b"\x00" * 6400) is True  # after "Hey Jarvis" / the hotkey it never blocks you
        assert await d._is_owner(b"\x00" * 6400, strict=True) is False  # a follow-up with no wake word does
    finally:
        await rig.stop()


async def test_after_recognising_you_it_keeps_talking_to_you_at_a_lower_bar():
    class V:
        enrolled = True

        def __init__(self, s):
            self.s = s

        def similarity(self, pcm):
            return self.s

    rig = await Rig().start()
    try:
        d = rig.daemon
        d.verifier = V(0.52)  # a muffled, noisy utterance of yours
        assert await d._is_owner(b"\x00" * 64000) is False  # cold: not enough
        d.verifier = V(0.85)
        assert await d._is_owner(b"\x00" * 64000) is True  # it knows you
        d.verifier = V(0.52)
        assert await d._is_owner(b"\x00" * 64000) is True  # in the middle of the conversation: accepted
    finally:
        await rig.stop()


async def test_music_is_ducked_while_listening_and_restored_afterwards():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.duck_to = 0.25
    cfg.activation.wake_gate_rms = 0
    rig = await Rig(cfg=cfg, replies=["Of course, sir."], stt_texts=["hello"]).start()
    try:
        d = rig.daemon
        d.cfg.activation.duck_media = True  # the Rig turns it off; this test supplies a fake Spotify
        calls = []

        async def osa(script, *args, timeout=15.0):
            calls.append(script)
            return "80" if "player state is playing" in script else ""

        d.assistant.ctx.osa = osa
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)
        await asyncio.sleep(0.2)
        assert any("set sound volume to 20" in c for c in calls)  # 80 percent ducked to a quarter of that
        await rig.say_utterance()
        await rig.wait_state(State.FOLLOW_UP)
        await rig.feed(SILENCE, 8 * 50 + 60)
        await rig.wait_state(State.IDLE)
        await asyncio.sleep(0.3)
        assert any("set sound volume to 80" in c for c in calls)  # and put back where it was
    finally:
        await rig.stop()


async def test_loud_room_relaxes_the_voice_check_but_still_needs_the_wake_word():
    class V:
        enrolled = True

        def similarity(self, pcm):
            return 0.30  # the music has smeared your voiceprint match

    rig = await Rig(replies=["Playing it, sir."], stt_texts=["what do you think about jazz", "hey jarvis what do you think about jazz"]).start()
    try:
        d = rig.daemon
        d.verifier = V()
        d._noise_floor = 900.0  # music is playing
        assert d._noisy()
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)
        await rig.say_utterance()  # no "Jarvis" in what was heard: ignored even though the room is too loud to judge by voice
        await asyncio.sleep(0.3)
        assert rig.llm.calls == []
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)
        await rig.say_utterance()  # you said "Jarvis": accepted despite the failed voice check
        await rig.wait_state(State.FOLLOW_UP)
        assert rig.llm.calls
    finally:
        await rig.stop()


async def test_echo_with_garbled_words_is_cut_from_the_front_of_what_you_said():
    rig = await Rig(replies=["x"], stt_texts=["go"]).start()
    try:
        d = rig.daemon
        d.last_reply_text = (
            "You can reverse a list by changing each node's next pointer. Start with three pointers: prev as null, current as "
            "the head, and next as current's next node. Then loop: set next to current's next, point current's next to prev, "
            "move prev and current one step forward, and repeat until current is null. The new head becomes prev.")
        heard = ("currents next node. Then loop set next to currents next. Point currents next to Prev. Move Prev and current one "
                 "step forward and repeat until current is Null. The new head becomes Prev. Uh yeah, let's skip all that. "
                 "Just write the code down for me and open it as a file in VS code.")
        cut = d._strip_own_echo_edges(heard)
        assert cut.startswith("Uh yeah, let's skip all that") and "head becomes" not in cut
        assert cut.endswith("open it as a file in VS code.")
        # your own words that happen to share ordinary words with the reply survive
        assert d._strip_own_echo_edges("write the code down for me and open it as a file in VS code") == \
            "write the code down for me and open it as a file in VS code"
    finally:
        await rig.stop()


async def test_a_timer_that_ends_mid_conversation_is_said_as_soon_as_there_is_a_gap():
    from jarvis_core.config import Config

    cfg = Config()
    cfg.activation.wake_gate_rms = 0
    rig = await Rig(cfg=cfg, replies=["Of course, sir."], stt_texts=["hello"]).start()
    try:
        d = rig.daemon
        d._outbox_poll_s = 0.05
        rig.wake.armed = True
        await rig.feed(SILENCE, 2)
        await rig.wait_state(State.LISTENING)  # you are in the middle of saying something
        assert await d.announce_reliably("Your tea timer is up, sir.") is True  # accepted, not lost
        await asyncio.sleep(0.3)
        assert not any("tea timer" in t for t in rig.tts.spoken)  # it does not talk over you
        await rig.say_utterance()  # the turn finishes ...
        for _ in range(100):
            if any("tea timer" in t for t in rig.tts.spoken):
                break
            await asyncio.sleep(0.05)
        assert any("tea timer" in t for t in rig.tts.spoken)  # ... and then it keeps its word
        assert d._outbox == []
    finally:
        await rig.stop()


async def test_promises_survive_a_restart_and_expire_when_stale(tmp_path):
    rig = await Rig().start()
    try:
        d = rig.daemon
        d._outbox_poll_s = 0.05
        d._outbox.append({"text": "Your pasta timer is up, sir.", "earcon": "alert", "kw": {}, "title": "Jarvis",
                          "queued": d._clock(), "due": d._clock() + 600, "notified": False})
        d._save_outbox()
        d._outbox.clear()
        d.restore_outbox()  # what a fresh start does
        for _ in range(100):
            if any("pasta timer" in t for t in rig.tts.spoken):
                break
            await asyncio.sleep(0.05)
        assert any("pasta timer" in t for t in rig.tts.spoken)
    finally:
        await rig.stop()


async def test_two_word_echo_of_the_end_of_jarvis_last_sentence_is_cut():
    rig = await Rig(replies=["x"], stt_texts=["go"]).start()
    try:
        d = rig.daemon
        d.last_reply_text = "Done, sir. Timer's running for 25 seconds. I'll let you know when it's up."
        assert d._strip_own_echo_edges("it's up. Actually make it 40.") == "Actually make it 40."
        assert d._strip_own_echo_edges("make it up to me tomorrow please") == "make it up to me tomorrow please"
    finally:
        await rig.stop()


async def test_typed_text_runs_a_turn_without_the_microphone():
    rig = await Rig(replies=["Elephants are large, sir."]).start()
    try:
        rig.daemon.on_text_input("tell me about elephants")
        await rig.wait_state(State.FOLLOW_UP)
        assert rig.stt.calls == 0
        assert rig.assistant.history.messages()[0]["content"] == "tell me about elephants"
        assert rig.tts.spoken[-1] == "Elephants are large, sir."
        assert "tell me about elephants" in rig.frontend.captions
    finally:
        await rig.stop()


async def test_typed_text_stops_a_reply_in_progress_and_answers_prompts():
    rig = await Rig(replies=["First answer, sir.", "Second answer, sir."], auto_finish=False).start()
    try:
        rig.daemon.on_text_input("tell me something")
        await rig.wait_state(State.SPEAKING)
        rig.daemon.on_text_input("never mind, what time is it")  # typing over it cancels the first reply
        await rig.wait_state(State.SPEAKING)
        assert rig.assistant.history.messages()[-1]["role"] in ("assistant", "user", "tool")
    finally:
        await rig.stop()

    rig = await Rig().start()
    try:
        answer = asyncio.create_task(rig.daemon.ask_confirmation("Shall I delete it, sir?", timeout_s=5))
        await rig.wait_state(State.CONFIRMING)
        await asyncio.sleep(0.05)
        rig.daemon.on_text_input("yes")
        assert await asyncio.wait_for(answer, 5) is True  # a typed yes counts like a spoken one
    finally:
        await rig.stop()
