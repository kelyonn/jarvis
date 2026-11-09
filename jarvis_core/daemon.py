"""The Jarvis daemon: audio loop, state machine, turns, pause/resume, announcements."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from jarvis_core.briefing.store import briefing_day
from jarvis_core.config import Config
from jarvis_core.errors import LLMAuthError, LLMUnavailable
from jarvis_core.frontend import Frontend
from jarvis_core.interfaces import STT, TTS
from jarvis_core.policy.greeting import welcome_line
from jarvis_core.state import State, StateMachine
from jarvis_core.voice.audio_bus import PreRoll
from jarvis_core.voice.speaker import Speaker
from jarvis_core.voice.stt import filter_transcript
from jarvis_core.voice.turn import (
    is_sleep_phrase,
    is_stop_phrase,
    parse_confirmation,
    strip_wake_phrase,
)
from jarvis_core.voice.vad import (
    VAD,
    BargeInDetector,
    EndEvent,
    Endpointer,
    EndpointerParams,
)
from jarvis_core.voice.wake import WakeWordDetector

log = logging.getLogger(__name__)

_FOLLOWUP_PREFIX_BYTES = 12800  # 400 ms of 16 kHz PCM16
Hook = Callable[..., Awaitable[None]]


@dataclass
class Hooks:
    """Extension points used by briefings, alerts and policy (later phases)."""

    on_ready: list[Hook] = field(default_factory=list)
    on_pause: list[Hook] = field(default_factory=list)
    on_resume: list[Hook] = field(default_factory=list)  # called with paused_at: datetime | None
    on_cancel: list[Hook] = field(default_factory=list)  # hotkey long-press / "stop"
    on_idle: list[Hook] = field(default_factory=list)  # became free (deliver held alerts)
    on_device_usage: list[Callable[[bool, bool, str], None]] = field(default_factory=list)
    on_turn_done: list[Hook] = field(default_factory=list)  # called with (user_text, reply_text)
    on_command: dict[str, Hook] = field(default_factory=dict)  # menu / orb commands, e.g. "brief_now"


class Daemon:
    def __init__(
        self,
        cfg: Config,
        frontend: Frontend,
        *,
        assistant: Any,
        stt: STT,
        tts: TTS,
        wake: WakeWordDetector | None,
        vad_factory: Callable[[], VAD],
        hooks: Hooks | None = None,
        clock: Callable[[], float] = time.monotonic,
        strict_states: bool = False,
    ) -> None:
        self.cfg = cfg
        self.frontend = frontend
        self.assistant = assistant
        self.stt = stt
        self.tts = tts
        self.wake = wake
        self.vad = vad_factory()
        self._barge_vad = vad_factory()
        self.hooks = hooks or Hooks()
        self._clock = clock
        self.sm = StateMachine(strict=strict_states, on_change=self._on_state_change, clock=clock)
        self.speaker = Speaker(tts, frontend)
        self.preroll = PreRoll(3000)
        self.barge = BargeInDetector(threshold=0.8, sustain_ms=350.0)
        self._q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=250)
        self._audio_task: asyncio.Task | None = None
        self._turn_task: asyncio.Task | None = None
        self._utt = bytearray()
        self._endpointer: Endpointer | None = None
        self._followup = False
        self._ptt = False
        self._hotkey_down_at = 0.0
        self._capture: asyncio.Future[str | None] | None = None
        self._stt_pool = None
        self._stopped = asyncio.Event()
        self._sleep_listen = False  # asleep, but the mic stays open for "Hey Jarvis" / the hotkey
        self._mute_flag = False  # "go to sleep" / menu Off: wake word ignored, hotkey still works
        self._mute_until: float | None = None  # wall-clock expiry for "pause for an hour"
        self._mute_timer: asyncio.TimerHandle | None = None
        self._mute_loaded = False
        self._last_heard, self._last_heard_at, self._same_count = "", 0.0, 0
        self._deaf_frames = 0
        self.verifier = None  # SpeakerVerifier once voice recognition is available
        self._outbox: list[dict] = []  # things Jarvis has promised to tell you (timers, reminders) and has not said yet
        self._outbox_task: asyncio.Task | None = None
        self._outbox_poll_s = 1.0
        self._enroll = None  # an EnrollSession while Jarvis is teaching itself your voice
        self._wake_score = 1.0
        self._dead_windows = 0
        self._base_wake_threshold = float(getattr(wake, "threshold", 0.5)) if wake is not None else 0.5
        self._ducked: dict[str, int] = {}
        self._last_owner_at = -1e9
        self._last_welcome = -1e9
        self._welcome_day = None
        self._announce_task: asyncio.Task | None = None
        self._last_capture_audio = b""
        self._utt_trigger = ""
        self._barge_block_until = 0.0
        self.last_speaker_score: float | None = None
        self._followup_until = 0.0
        self._noise_floor = 0.0
        self._gate_hang = 0
        self._gate_was_open = True
        self._headphones = False  # output is headphones/earbuds: no speaker echo, so voice barge-in is safe
        self._headphones_checked_at = -1e9
        self._stat_at, self._stat_sq, self._stat_n, self._stat_peak, self._stat_wake = clock(), 0.0, 0, 0, 0.0
        self._asleep = False
        self._locked = False
        self._capture_open = False  # whether we've asked the frontend to hold the microphone open
        self.paused_at: datetime | None = None
        self.mic_in_use_by_other = False
        self.cu_active = False  # a computer-use run is in progress: keep listening for "stop"
        self.camera_in_use = False
        self.last_user_text = ""
        self.last_reply_text = ""
        self.last_activity = clock()
        self._tasks: set[asyncio.Task] = set()
        frontend.events = self

    # ------------------------------------------------------------------ run

    async def run(self) -> None:
        import concurrent.futures

        self._stt_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="jarvis-stt")
        await self.frontend.start()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._stt_pool, self._warm_up)
        self._audio_task = asyncio.create_task(self._audio_loop(), name="audio-loop")
        self.sm.go(State.IDLE)
        if self._muted:  # restored from a previous run: stay off until the hotkey / menu turns it on
            await self.frontend.set_mic_enabled(False)
            self._arm_mute_timer()
        self.restore_outbox()
        log.info("Jarvis is ready")
        for h in self.hooks.on_ready:
            await self._safe(h)
        await self._stopped.wait()
        await self.shutdown()

    def _warm_up(self) -> None:
        for obj in (self.stt, self.tts):
            warm = getattr(obj, "warm_up", None)
            if warm:
                try:
                    warm()
                except Exception:
                    log.exception("warm-up failed for %s", type(obj).__name__)

    async def shutdown(self) -> None:
        await self._cancel_turn()
        if self._audio_task:
            self._audio_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._audio_task
        await self.frontend.stop()

    def request_stop(self) -> None:
        self._stopped.set()

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception())

    async def _safe(self, fn: Hook, *a: Any) -> None:
        try:
            await fn(*a)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hook %s failed", getattr(fn, "__name__", fn))

    # --------------------------------------------------------- state / UI

    def _on_state_change(self, old: State, new: State) -> None:
        log.debug("state %s -> %s", old.value, new.value)
        self.last_activity = self._clock()
        self._spawn(self.frontend.set_state(new.value))
        if new in (State.THINKING, State.SPEAKING, State.ANNOUNCING):
            self._spawn(self._refresh_output_kind())
        self._sync_capture()
        if new == State.IDLE and old != State.IDLE:
            self._spawn(self._run_idle_hooks())
        if self.cfg.activation.duck_media:
            if new in (State.LISTENING, State.FOLLOW_UP):
                self._spawn(self._duck_media())
            elif new in (State.IDLE, State.PAUSED):
                self._spawn(self._restore_media())

    # ------------------------------------------------ ducking the music while you talk

    _DUCK_APPS = ("Spotify", "Music")

    async def _duck_media(self) -> None:
        """Lower Spotify / Apple Music while Jarvis listens so your voice is clear; put it back when the turn ends."""
        ctx = getattr(self.assistant, "ctx", None)
        if ctx is None or self._ducked:
            return
        for app in self._DUCK_APPS:
            try:
                out = await ctx.osa(
                    f'if application "{app}" is running then\ntell application "{app}"\n'
                    'if player state is playing then return (sound volume as string)\nend tell\nend if\nreturn ""',
                    timeout=4.0)
                if not out.strip():
                    continue
                vol = int(float(out.strip()))
                target = max(5, int(vol * self.cfg.activation.duck_to))
                await ctx.osa(f'tell application "{app}" to set sound volume to {target}', timeout=4.0)
                self._ducked[app] = vol
            except Exception:  # noqa: BLE001 - ducking is a courtesy, never an error
                log.debug("could not duck %s", app, exc_info=True)

    async def _restore_media(self) -> None:
        ctx = getattr(self.assistant, "ctx", None)
        if ctx is None or not self._ducked:
            return
        ducked, self._ducked = self._ducked, {}
        for app, vol in ducked.items():
            try:
                await ctx.osa(f'tell application "{app}" to set sound volume to {vol}', timeout=4.0)
            except Exception:  # noqa: BLE001
                log.debug("could not restore %s", app, exc_info=True)

    # ------------------------------------------------ mute (persisted across restarts)

    @property
    def _muted(self) -> bool:
        if not self._mute_loaded:
            self._load_mute()
        if self._mute_flag and self._mute_until is not None and time.time() >= self._mute_until:
            self._mute_flag, self._mute_until = False, None
            self._save_mute()
        return self._mute_flag

    @_muted.setter
    def _muted(self, value: bool) -> None:
        self._mute_loaded = True
        self._mute_flag = bool(value)
        self._sleep_listen = False
        self._mute_until = None
        self._save_mute()
        self._arm_mute_timer()

    @property
    def _wake_blocked(self) -> bool:
        """Muted for real (menu Off, timed pause)? Asleep-by-voice still hears "Hey Jarvis"."""
        return self._muted and not self._sleep_listen

    def _go_to_sleep(self) -> None:
        self._muted = True
        self._sleep_listen = True

    def pause_for(self, minutes: float) -> None:
        """Ignore the wake word for a while; it comes back by itself (and survives a restart)."""
        self._mute_loaded = True
        self._mute_flag = True
        self._sleep_listen = False
        self._mute_until = time.time() + minutes * 60
        self._save_mute()
        self._arm_mute_timer()
        self._spawn(self.frontend.set_mic_enabled(False))
        self._sync_capture()

    def _mute_file(self):
        from jarvis_core import paths

        return paths.home() / "mute.json"

    def _load_mute(self) -> None:
        self._mute_loaded = True
        try:
            d = json.loads(self._mute_file().read_text())
            self._mute_flag = bool(d.get("muted"))
            until = d.get("until")
            self._mute_until = float(until) if until else None
            if self._mute_flag and self._mute_until is not None and time.time() >= self._mute_until:
                self._mute_flag, self._mute_until = False, None
        except (OSError, ValueError, TypeError):
            self._mute_flag, self._mute_until = False, None

    def _save_mute(self) -> None:
        try:
            if self._mute_flag:
                self._mute_file().write_text(json.dumps({"muted": True, "until": self._mute_until}))
            else:
                self._mute_file().unlink(missing_ok=True)
        except OSError:
            log.warning("could not persist mute state", exc_info=True)

    def _arm_mute_timer(self) -> None:
        if self._mute_timer:
            self._mute_timer.cancel()
            self._mute_timer = None
        if self._mute_flag and self._mute_until is not None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._mute_timer = loop.call_later(max(0.0, self._mute_until - time.time()) + 0.05, self._mute_expired)

    def _mute_expired(self) -> None:
        if not self._muted:  # property clears the expired flag
            self._spawn(self.frontend.set_mic_enabled(True))
            self._sync_capture()

    def _wants_capture(self) -> bool:
        """Should the microphone be open right now? Only while we're actually listening, unless wake-word mode."""
        st = self.sm.state
        if st in (State.PAUSED, State.BOOTING) or self._asleep or self._locked:
            return False
        if self._capture is not None or st in (State.LISTENING, State.FOLLOW_UP):
            return True
        a = self.cfg.activation
        if self._voice_barge_ok() and (st in (State.SPEAKING, State.ANNOUNCING) or (st == State.THINKING and self.cu_active)):
            return True
        if a.listen_mode == "wake_word" and not self._wake_blocked:
            if st == State.IDLE:
                return True
            # Siri-style: stay able to hear "Hey Jarvis" while it thinks and talks, so you can interrupt it. The mic is
            # held for the whole turn on purpose: closing it between steps makes Bluetooth earbuds flip between call and
            # music mode, which cuts the reply off mid-sentence.
            if a.wake_interrupt and self.wake:
                return True
        return False

    def _voice_barge_ok(self) -> bool:
        a = self.cfg.activation
        if not a.barge_in:
            return False
        # Speakers have no echo cancellation: Jarvis's own voice would trip it, so there only an enrolled voiceprint
        # (checked in _is_owner before anything is interrupted) can be trusted to tell you from Jarvis.
        return self.frontend.supports_aec or self._headphones or (a.barge_in_speakers and self._speaker_check_on())

    async def _refresh_output_kind(self) -> None:
        """Is audio going to headphones/earbuds? Then the mic can't hear Jarvis, so talking over it can interrupt."""
        if self._clock() - self._headphones_checked_at < 15:
            return
        self._headphones_checked_at = self._clock()
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: __import__("subprocess").run(
                        ["system_profiler", "SPAudioDataType", "-json"], capture_output=True, text=True, timeout=8
                    ).stdout
                ),
                10,
            )
            items = json.loads(res)["SPAudioDataType"][0]["_items"]
        except Exception:
            return
        phones = False
        for d in items:
            if d.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                transport = str(d.get("coreaudio_device_transport", ""))
                name = str(d.get("_name", "")).lower()
                phones = any(k in transport for k in ("bluetooth", "usb")) or "headphone" in name
        if phones != self._headphones:
            log.info("output is %s: voice barge-in %s", "headphones" if phones else "speakers",
                     "on" if phones else "off (say Hey Jarvis to interrupt)")
        self._headphones = phones
        self._sync_capture()

    def _sync_capture(self) -> None:
        want = self._wants_capture()
        if want == self._capture_open:
            return
        self._capture_open = want
        if not want:
            self.preroll.clear()  # whatever was heard before closing (maybe our own voice) must not leak into the next turn
        self._spawn(self.frontend.set_capture(want))

    async def _run_idle_hooks(self) -> None:
        for h in self.hooks.on_idle:
            await self._safe(h)

    @property
    def state(self) -> State:
        return self.sm.state

    @property
    def is_free(self) -> bool:
        """Idle enough to accept an announcement."""
        return self.sm.state in (State.IDLE, State.FOLLOW_UP, State.RESUMING) and self._capture is None

    # --------------------------------------------------- FrontendEvents API

    def on_audio(self, frame: bytes) -> None:
        if self._q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._q.get_nowait()
        self._q.put_nowait(frame)

    def on_hotkey(self, action: str) -> None:
        self._spawn(self._hotkey(action))

    def on_lifecycle(self, event: str) -> None:
        self._spawn(self._lifecycle(event))

    def on_device_usage(self, mic_in_use_by_other: bool, camera_in_use: bool, app_hint: str) -> None:
        log.info("device usage: mic by other=%s camera=%s (%s)", mic_in_use_by_other, camera_in_use, app_hint)
        self.mic_in_use_by_other = mic_in_use_by_other
        self.camera_in_use = camera_in_use
        for cb in self.hooks.on_device_usage:
            try:
                cb(mic_in_use_by_other, camera_in_use, app_hint)
            except Exception:
                log.exception("device usage hook failed")
        if not (mic_in_use_by_other or camera_in_use):
            self._spawn(self._run_idle_hooks())

    def on_orb_click(self, action: str) -> None:
        if action == "mute_toggle":
            self._muted = not self._muted
            self._spawn(self.frontend.set_mic_enabled(not self._muted))
            self._sync_capture()
        elif action == "pause_hour":
            self.pause_for(60)
        elif action == "voice_id_forget":
            if self.verifier is not None:
                self.verifier.forget()
                log.info("voiceprint removed from the menu: Jarvis responds to any voice again")
        elif action in ("dismiss", "kill"):
            self._spawn(self._hotkey("long_press"))
        elif action in self.hooks.on_command:
            self._spawn(self._safe(self.hooks.on_command[action]))
        else:
            for h in self.hooks.on_idle:
                self._spawn(self._safe(h))

    def on_text_input(self, text: str) -> None:
        """Something typed into the orb panel: the same as saying it, minus the microphone."""
        text = " ".join((text or "").split())[:2000]
        if text:
            self._spawn(self._typed_turn(text))

    async def _typed_turn(self, text: str) -> None:
        cap = self._capture
        if cap is not None and not cap.done():  # Jarvis is waiting for an answer (a yes/no, a choice): this is it
            await self.frontend.caption(text, True)
            cap.set_result(text)
            return
        st = self.sm.state
        if st in (State.PAUSED, State.BOOTING, State.RESUMING):
            return
        if self._muted:
            self._muted = False
            await self.frontend.set_mic_enabled(True)
            self._sync_capture()
        if st not in (State.IDLE, State.FOLLOW_UP):  # typing over a reply or an alert stops it, like speaking over it
            await self._cancel_turn()
            if st in (State.SPEAKING, State.ANNOUNCING):
                self._note_interruption()
            await self.frontend.stop_audio()
        st = self.sm.state
        if st == State.ANNOUNCING:
            self.sm.go(State.IDLE)
        elif st == State.FOLLOW_UP:
            self.sm.go(State.LISTENING)
            self.sm.go(State.TRANSCRIBING)
        try:
            text = strip_wake_phrase(text) or text
            if is_stop_phrase(text):
                await self.frontend.earcon("done")
                self.sm.go(State.IDLE)
            elif is_sleep_phrase(text):
                self._go_to_sleep()
                await self.speaker.speak("Very good, sir. I'll stay quiet until you call.")
                self.sm.go(State.IDLE)
            else:
                log.info("typed: %r", text)
                await self._run_turn(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("typed turn failed")
            await self.frontend.earcon("error")
            if self.sm.state not in (State.PAUSED,):
                self.sm.go(State.IDLE)

    def on_connected(self, info: dict[str, Any]) -> None:
        log.info("frontend connected: %s", {k: v for k, v in info.items() if k != "token"})

    def on_disconnected(self) -> None:
        log.warning("frontend disconnected")

    # ------------------------------------------------------- audio loop

    async def _audio_loop(self) -> None:
        while True:
            frame = await self._q.get()
            try:
                await self._handle_frame(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("audio loop error")

    def _mic_stats(self, frame: bytes) -> float:
        """Input level of this frame (rms). Every 10 s the log gets level and best wake score, which diagnoses dead mics."""
        import numpy as np

        x = np.frombuffer(frame[: len(frame) // 2 * 2], dtype=np.int16)
        rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) if x.size else 0.0
        self._stat_sq += rms * rms
        self._stat_n += 1
        self._stat_peak = max(self._stat_peak, int(np.abs(x).max()) if x.size else 0)
        self._stat_wake = max(self._stat_wake, float(getattr(self.wake, "last_score", 0.0)))
        now = self._clock()
        if now - self._stat_at >= 10:
            if self._stat_n:
                log.info("mic: %d frames, rms=%.0f peak=%d, best wake score=%.2f (threshold %.2f)",
                         self._stat_n, (self._stat_sq / self._stat_n) ** 0.5, self._stat_peak, self._stat_wake,
                         self.cfg.activation.wake_threshold)
            self._check_dead_mic()
            self._stat_at, self._stat_sq, self._stat_n, self._stat_peak, self._stat_wake = now, 0.0, 0, 0, 0.0
        return rms

    def _check_dead_mic(self) -> None:
        """A microphone that delivers only zeros (earbuds that reconnected, a device that vanished) never fails loudly.
        If a whole 10 s window was silent, close and reopen the capture device."""
        if self._stat_n and self._stat_peak == 0 and self._capture_open and not self._muted:
            self._dead_windows += 1
            if self._dead_windows >= 1:
                log.warning("the microphone is delivering silence; reopening it")
                self._dead_windows = 0

                async def reopen() -> None:
                    await self.frontend.set_capture(False)
                    await asyncio.sleep(0.8)
                    await self.frontend.set_capture(self._wants_capture())

                self._spawn(reopen())
        else:
            self._dead_windows = 0

    def _noisy(self) -> bool:
        """Is something loud playing nearby (music, a TV, a video)? Judged from the background level."""
        return self._noise_floor >= self.cfg.activation.noisy_floor_rms

    def _wake_gate_open(self, rms: float) -> bool:
        """Quiet-room gate: True when the wake-word model should look at this frame."""
        limit = self.cfg.activation.wake_gate_rms
        if limit <= 0:
            return True
        if self._noise_floor <= 0.0:
            self._noise_floor = limit / 2  # a first loud frame must not be mistaken for the room's normal level
        if rms < self._noise_floor:
            self._noise_floor = 0.9 * self._noise_floor + 0.1 * rms
        else:
            self._noise_floor = 0.996 * self._noise_floor + 0.004 * rms  # follows a noisy room within ~15 s
        if rms > max(limit, self._noise_floor * 2.2):
            self._gate_hang = 60  # keep looking for 1.2 s after the last loud frame
        if self._gate_hang > 0:
            self._gate_hang -= 1
            return True
        return False

    async def _handle_frame(self, frame: bytes) -> None:
        st = self.sm.state
        self.preroll.push(frame)
        rms = self._mic_stats(frame)
        if st == State.IDLE:
            if self.wake and not self._wake_blocked:
                was_open = self._gate_was_open
                self._gate_was_open = self._wake_gate_open(rms)
                if hasattr(self.wake, "threshold"):
                    self.wake.threshold = self._base_wake_threshold * (0.7 if self._noisy() else 1.0)
                if self._gate_was_open:
                    if not was_open and self.cfg.activation.wake_gate_rms > 0:
                        self.wake.reset()  # stale context from before the quiet spell
                        fired = self.wake.process(self.preroll.tail(800))  # replay the start of the sound
                    else:
                        fired = self.wake.process(frame)
                    if fired and await self._wake_is_owner():
                        self._wake_score = float(getattr(self.wake, "fired_score", 1.0))
                        # start the recording ~2 s back so it contains "Hey Jarvis" and the transcript can confirm it
                        await self._begin_listening("wake", preroll_ms=2200)
        elif st in (State.LISTENING, State.FOLLOW_UP) or (st == State.CONFIRMING and self._capture):
            await self._listen_frame(frame)
        elif st in (State.SPEAKING, State.ANNOUNCING) or (st == State.THINKING and self.cu_active):
            a = self.cfg.activation
            if (
                a.listen_mode == "wake_word"
                and a.wake_interrupt
                and self.wake
                and not self._muted
                and self.wake.process(frame)
            ):
                await self._wake_interrupt()
            elif self._voice_barge_ok() and (st in (State.SPEAKING, State.ANNOUNCING) or self.cu_active):
                for p in self._barge_vad.feed(frame):
                    if self.barge.feed(p) and self._clock() >= self._barge_block_until:
                        self.barge.reset()
                        if await self._is_owner(self.preroll.tail(700), barge=True):
                            await self._barge_in()
                        else:
                            self._barge_block_until = self._clock() + 1.0  # not you (or Jarvis's own leak): carry on talking
                        break

    # ------------------------------------------------------- listening

    async def _begin_listening(
        self,
        trigger: str,
        *,
        followup: bool = False,
        preroll_ms: int = 300,
        no_speech_timeout_s: float | None = None,
    ) -> None:
        a = self.cfg.activation
        self.vad.reset()
        self._barge_vad.reset()
        if self.wake:
            self.wake.reset()
        timeout = no_speech_timeout_s or (a.follow_up_seconds if followup else a.no_speech_timeout_s)
        self._endpointer = Endpointer(
            EndpointerParams(
                end_silence_ms=a.follow_up_end_silence_ms if followup else a.end_silence_ms,
                no_speech_timeout_s=timeout,
                max_s=a.max_utterance_s,
            )
        )
        self._deaf_frames = 0
        if followup:
            self._deaf_frames = 25  # 0.5 s of audio: the tail of Jarvis's own voice (and Bluetooth lag) is still in the air
        self._utt = bytearray(b"" if followup else self.preroll.tail(preroll_ms))
        self._followup = followup
        self._utt_trigger = trigger
        self._ptt = trigger == "hotkey"
        if self._capture is None:
            self.sm.go(State.FOLLOW_UP if followup else State.LISTENING)
        self._sync_capture()
        if trigger in ("wake", "hotkey"):
            await self.frontend.earcon("listen")

    async def _listen_frame(self, frame: bytes) -> None:
        ep = self._endpointer
        if ep is None or ep.done:
            return
        if self._deaf_frames > 0:
            self._deaf_frames -= 1
            return
        self._utt.extend(frame)
        if self._followup and not ep.started and len(self._utt) > _FOLLOWUP_PREFIX_BYTES:
            del self._utt[:-_FOLLOWUP_PREFIX_BYTES]
        for p in self.vad.feed(frame):
            ev = ep.feed(p)
            if ev == EndEvent.SPEECH_START and self.sm.state == State.FOLLOW_UP:
                self.sm.go(State.LISTENING)
            elif ev in (EndEvent.SPEECH_END, EndEvent.MAX_LENGTH):
                await self._end_utterance()
                return
            elif ev == EndEvent.NO_SPEECH_TIMEOUT:
                await self._no_speech()
                return

    async def start_enrollment(self) -> None:
        """Start teaching Jarvis your voice (``jarvisd voice-id enroll``): speak the first phrase, then wait for you."""
        from jarvis_core.voice import enroll

        text = enroll.begin(self)
        await self.announce(text, remember=False, follow_up=False)
        if self._enroll is not None:
            await self._open_followup(enroll.WINDOW_S)

    async def _open_followup(self, window: float) -> None:
        self._followup_until = self._clock() + window
        if self.sm.state == State.IDLE:
            self.sm.go(State.ANNOUNCING)  # the state machine only reaches follow-up from a spoken state
        self.sm.go(State.FOLLOW_UP)
        await self._begin_listening("followup", followup=True, no_speech_timeout_s=window)

    async def _enroll_turn(self, audio: bytes) -> None:
        """A phrase for the voice-learning session: check it, say what comes next, and keep listening."""
        from jarvis_core.voice import enroll

        session = self._enroll
        if session is None or session.expired:
            self._enroll = None
            self.sm.go(State.IDLE)
            return
        text = ""
        try:
            text = await self._transcribe(audio)
        except Exception:  # noqa: BLE001
            log.exception("transcription failed during voice enrolment")
        reply = await enroll.step(self, audio, text)
        self.sm.go(State.THINKING)
        self.sm.go(State.SPEAKING)
        await self.speaker.speak(reply)
        if self._enroll is not None:
            await self._open_followup(enroll.WINDOW_S)
        else:
            self.sm.go(State.IDLE)

    async def _resume_followup_or_idle(self) -> None:
        """A false alarm (echo of Jarvis's own voice, noise, an empty transcript) must not end the conversation:
        if the follow-up window is still open, keep listening for what you actually say."""
        remaining = self._followup_until - self._clock()
        if remaining > 1.0 and self._capture is None:
            await self._begin_listening("followup", followup=True, no_speech_timeout_s=remaining)
        else:
            self.sm.go(State.IDLE)

    async def _no_speech(self) -> None:
        self._endpointer = None
        if self._capture is not None:
            if not self._capture.done():
                self._capture.set_result(None)
            return
        if self.sm.state == State.LISTENING:
            await self.frontend.earcon("done")
        self.sm.go(State.IDLE)

    async def _end_utterance(self) -> None:
        audio = bytes(self._utt)
        self._utt = bytearray()
        self._endpointer = None
        if self._capture is not None:
            self._last_capture_audio = audio
            asyncio.create_task(self._transcribe_for_capture(audio))
            return
        self.sm.go(State.TRANSCRIBING)
        self._turn_task = asyncio.create_task(self._process_utterance(audio), name="turn")

    def _speaker_check_on(self) -> bool:
        return (
            self.verifier is not None
            and self.verifier.enrolled
            and self.cfg.voice.speaker_verify
            and self._enroll is None
        )

    async def _is_owner(self, audio: bytes, *, barge: bool = False, threshold: float | None = None, strict: bool = False) -> bool:
        """True when the audio sounds like the enrolled owner (or cannot be judged, which never blocks you).

        Lenient on purpose: right after it has recognised you it keeps listening at a lower bar, and every clear match
        refines the voiceprint, so it adapts to how you sound through this particular microphone."""
        if not self._speaker_check_on():
            return True
        v = self.cfg.voice
        threshold = threshold if threshold is not None else (v.speaker_barge_threshold if barge else v.speaker_threshold)
        if self._clock() - self._last_owner_at < v.speaker_grace_s:
            threshold = min(threshold, v.speaker_grace_threshold)
        verifier = self.verifier
        checker = getattr(verifier, "check", None)
        if checker is not None:
            sim, emb = await asyncio.to_thread(checker, audio)
        else:  # a verifier that only reports similarity
            sim, emb = await asyncio.to_thread(verifier.similarity, audio), None  # type: ignore[union-attr]
        if sim is None:
            # too short or too quiet to judge. After "Hey Jarvis" or a hotkey press that never blocks you; for a follow-up
            # with no wake word it does, because a TV or a video would otherwise slip through exactly that gap.
            if strict:
                log.info("speaker check: clip too short to judge, not acting on it without the wake word")
            return not strict
        self.last_speaker_score = sim
        ok = sim >= threshold
        log.info("speaker check: %.2f (%s, need %.2f)", sim, "you" if ok else "not you", threshold)
        if ok:
            self._last_owner_at = self._clock()
            if v.speaker_adapt and emb is not None and not barge and sim >= 0.75:
                verifier.adapt(emb)  # type: ignore[union-attr]
        return ok

    async def _transcribe(self, audio: bytes) -> str:
        loop = asyncio.get_running_loop()
        t = await loop.run_in_executor(self._stt_pool, self.stt.transcribe, audio, 16000)
        return filter_transcript(t, len(audio) / 32000)

    async def _transcribe_for_capture(self, audio: bytes) -> None:
        try:
            if not await self._is_owner(audio):
                text = ""
            else:
                text = await self._transcribe(audio)
        except Exception:
            log.exception("transcription failed")
            text = ""
        if self._capture and not self._capture.done():
            self._capture.set_result(text or None)

    async def listen_once(self, timeout_s: float = 10.0) -> str | None:
        """Capture one utterance without starting a turn (used for confirmations)."""
        loop = asyncio.get_running_loop()
        self._capture = loop.create_future()
        try:
            await self._begin_listening("capture", followup=True, no_speech_timeout_s=timeout_s)
            return await asyncio.wait_for(self._capture, timeout_s + 15)
        except asyncio.TimeoutError:
            return None
        finally:
            self._capture = None
            self._endpointer = None
            self._sync_capture()

    async def ask_confirmation(self, prompt: str, timeout_s: float = 10.0) -> bool:
        """Speak ``prompt`` and wait for a spoken yes/no. Silence or unclear counts as no."""
        prev = self.sm.state
        self.sm.go(State.CONFIRMING)
        try:
            await self.speaker.speak(prompt, interruptible=False)
            answer = parse_confirmation(await self.listen_once(timeout_s))
            if answer is None:
                await self.speaker.speak("I'll take that as a no.", interruptible=False)
                return False
            return answer
        finally:
            if self.sm.state == State.CONFIRMING:
                self.sm.go(State.THINKING if prev in (State.THINKING, State.SPEAKING) else State.IDLE)

    # --------------------------------------------------------- turns

    async def _process_utterance(self, audio: bytes) -> None:
        try:
            if self._enroll is not None:
                await self._enroll_turn(audio)
                return
            # Pressing the hotkey means you are at the keyboard: never lock yourself out of your own Jarvis.
            loud_unsure = False
            self.last_speaker_score = None  # set again by the check below
            if self._utt_trigger != "hotkey" and not await self._is_owner(
                audio, barge=self._utt_trigger == "barge", strict=self._utt_trigger == "followup"
            ):
                if self._noisy() and self._utt_trigger != "barge":
                    loud_unsure = True  # loud room: do not judge by voice, require that you actually said "Jarvis"
                else:
                    score = self.last_speaker_score or 0.0
                    if self._utt_trigger != "barge" and score >= self.cfg.voice.speaker_threshold - 0.15:
                        await self.frontend.caption(
                            f"I wasn't sure that was you (match {score:.2f}). Say it again, or press Option+Space.", False)
                    await self._resume_followup_or_idle()
                    return
            try:
                text = await self._transcribe(audio)
            except Exception:
                log.exception("transcription failed")
                await self.frontend.earcon("error")
                self.sm.go(State.IDLE)
                return
            if loud_unsure and text:
                from jarvis_core.voice.turn import mentions_wake_word

                if not mentions_wake_word(text):
                    log.info("loud background and no wake word in %r: ignored", text[:60])
                    await self._resume_followup_or_idle()
                    return
            if text and self._utt_trigger == "wake" and not self._wake_confirmed(text):
                log.info("false wake ignored (score %.2f, no wake word in %r)", self._wake_score, text[:60])
                self.sm.go(State.IDLE)
                return
            if self._utt_trigger == "wake" and self._muted:  # "Hey Jarvis" wakes it from sleep
                self._muted = False
                await self.frontend.set_mic_enabled(True)
                self._sync_capture()
            text = strip_wake_phrase(text) if text else text
            if not text:
                await self._resume_followup_or_idle()
                return
            from jarvis_core.voice.stt import has_repetition_loop

            if has_repetition_loop(text):
                log.info("ignored a repeating transcript (recogniser loop): %r", text[:60])
                await self._resume_followup_or_idle()
                return
            text = self._strip_own_echo_edges(text)
            if self._is_own_echo(text):
                log.info("ignored echo of my own voice: %r", text[:80])
                await self._resume_followup_or_idle()
                return
            log.info("heard: %r", text)
            if is_stop_phrase(text):
                await self.frontend.earcon("done")
                self.sm.go(State.IDLE)
                return
            if is_sleep_phrase(text):
                self._go_to_sleep()
                await self.speaker.speak("Very good, sir. I'll stay quiet until you call.")
                self.sm.go(State.IDLE)
                return
            await self._run_turn(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("turn failed")
            await self.frontend.earcon("error")
            if self.sm.state not in (State.PAUSED,):
                self.sm.go(State.IDLE)

    _DISMISS = re.compile(
        r"^(?:(?:hey |ok |okay )?jarvis[,.]? )?(?:please )?(?:"
        r"(?:close|hide|dismiss|exit|quit|stop|end|shut down|shut)(?: (?:jarvis|the chat|the conversation|chat|conversation|this|it|everything|talking|listening))?"
        r"|go away|that'?s all|that is all|never ?mind|nothing|leave me alone"
        r")(?: please)?(?: jarvis)?[.!]?$"
    )

    @classmethod
    def is_dismiss(cls, text: str) -> bool:
        t = " ".join(text.lower().replace("'", "'").split())
        return bool(cls._DISMISS.match(t)) or bool(re.match(r"^jarvis[,.]? (?:close|hide|dismiss|stop|exit|quit)\b.{0,24}$", t))

    async def _run_turn(self, text: str) -> None:
        if self.is_dismiss(text):  # "close Jarvis": stop whatever it is doing, hide and clear the chat, wait for the wake word
            log.info("dismissed by voice/typing: %r", text)
            await self.frontend.stop_audio()
            await self.frontend.hide_ui()
            self._followup_until = 0.0
            self.sm.go(State.IDLE)
            return
        self._followup_until = 0.0
        self.last_user_text = text
        self.sm.go(State.THINKING)
        await self.frontend.caption(text, True)
        reply = ""
        try:
            stream = self.assistant.respond_stream(text)
            self.speaker.cap_source = lambda: getattr(self.assistant, "speech_cap", None)
            reply = await self.speaker.speak_stream(
                stream, on_first_audio=lambda: self.sm.go(State.SPEAKING)
            )
        except LLMAuthError:
            reply = await self.speaker.speak(
                "I have no API key yet, sir. Run jarvisd set key to store it."
            )
        except LLMUnavailable as e:
            log.warning("LLM unavailable: %s", e)
            reply = await self.speaker.speak("I'm having trouble reaching my servers, sir.")
        self.last_reply_text = reply
        for h in self.hooks.on_turn_done:
            await self._safe(h, text, reply)
        outcome = getattr(self.assistant, "last_turn", None)
        if outcome and getattr(outcome, "mute", False):
            self._go_to_sleep()
            self._sync_capture()
        if outcome and (outcome.end_conversation or not outcome.follow_up):
            self.sm.go(State.IDLE)
        else:
            # The window adapts: a question needs time to answer, and a long reply takes longer to digest.
            window = self._followup_window(reply)
            self._followup_until = self._clock() + window
            self.sm.go(State.FOLLOW_UP)
            await self._begin_listening("followup", followup=True, no_speech_timeout_s=window)

    _WAITING_FOR_YOU = (
        "tell me when", "let me know", "once you", "when you're ready", "when you are ready", "go ahead", "your turn",
        "please say", "say yes", "say done", "reply with", "confirm", "which one", "what would you like",
    )

    def _followup_window(self, reply: str) -> float:
        """How long to wait for you after a reply. A human waits longer when they have just asked something or told
        you to do something, and moves on quickly after a plain statement."""
        base = float(self.cfg.activation.follow_up_seconds)
        hist = getattr(self.assistant, "history", None)
        if hist is not None and sum(1 for m in hist.messages() if m.get("role") == "user") >= 2:
            base = max(base, float(self.cfg.activation.conversation_follow_up_seconds))  # mid-conversation
        if self._enroll is not None:
            return 25.0
        text = reply.strip()
        tail = text[-220:].lower()
        if text.endswith("?") or any(cue in tail for cue in self._WAITING_FOR_YOU):
            return max(base, 20.0)  # a question, or an instruction you have to carry out first
        if len(text) > 280:
            return base + 4.0  # long replies take longer to take in
        return base

    async def _cancel_turn(self) -> None:
        t = self._turn_task
        self._turn_task = None
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        if self._capture and not self._capture.done():
            self._capture.set_result(None)
        at = self._announce_task
        if at is not None and not at.done():
            at.cancel()  # an alert or brief being read out stops too
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await at

    _STOPWORDS = frozenset(
        "a an the and or but to of in on at for with from by is are was were be been it its this that these those i you he "
        "she we they me my your our so as if then than not no yes do does did can could will would just about up down out".split()
    )

    def _strip_own_echo_edges(self, text: str) -> str:
        """When you speak over Jarvis (or its voice leaks into the mic), the recording starts or ends with part of its own
        sentence and your words are in the middle. Cut a leading or trailing run made of words Jarvis just said (tolerating a
        word the recogniser garbled: "current's" -> "currents"), keep the rest."""
        import re

        from jarvis_core.voice.text_norm import speakable

        def tok(w: str) -> str:
            return re.sub(r"[^a-z0-9]", "", w.lower())

        sources = [t for t in [self.last_reply_text, *self.speaker.spoken_text[-2:]] if t]
        said: set[str] = set()
        for t in sources:
            said.update(tok(w) for w in t.split())
            said.update(tok(w) for w in speakable(t).split())  # numbers and units as they were spoken
        said.discard("")
        words = text.split()
        if not said or len(words) < 5:
            return text

        def span(seq: list[str]) -> int:
            """How many leading words of ``seq`` are Jarvis's own (ends at the last real match before two misses in a row)."""
            end, misses = 0, 0
            for i, w in enumerate(seq):
                t = tok(w)
                if not t:
                    continue
                if t in said:
                    misses = 0
                    if t not in self._STOPWORDS:
                        end = i + 1  # only a meaningful word anchors the echo: "the" or "and" could be yours
                else:
                    misses += 1
                    if misses >= 2:
                        break
            return end

        lead = span(words)
        if lead < 3:
            # a short echo of the very end of Jarvis's last sentence ("... when it's up.") is still echo
            last = [tok(w) for w in (self.last_reply_text or "").split() if tok(w)]
            for k in (4, 3, 2):
                if len(words) > k + 1 and last[-k:] and [tok(w) for w in words[:k]] == last[-k:]:
                    lead = k
                    break
        if lead >= 2 and (lead >= 3 or [tok(w) for w in words[:lead]] == [tok(w) for w in (self.last_reply_text or "").split() if tok(w)][-lead:]):
            words = words[lead:]
        trail = span(list(reversed(words)))
        if trail >= 3:
            words = words[: len(words) - trail]
        rest = " ".join(words)
        # a remainder that is itself something Jarvis said (or a stray word or two) means the whole thing was echo
        if len(words) < 2 or all(tok(w) in said or not tok(w) for w in words):
            return text
        return rest

    def _is_own_echo(self, text: str) -> bool:
        """True when the transcript is Jarvis hearing itself (speaker or earbud leakage -> mic) or a line repeating.

        Both sides are put through the same speech-normaliser first, so "6.9%" and "2430" match what was actually said
        ("six point nine percent", "two thousand four hundred and thirty"). The heard words only need to be covered by
        what Jarvis said recently (it may have caught just the tail of a long reply)."""
        import re

        from rapidfuzz import fuzz

        from jarvis_core.voice.text_norm import speakable

        def norm(t: str) -> str:
            return re.sub(r"[^a-z0-9 ]", " ", speakable(t).lower().replace("-", " ")).strip()

        heard = " ".join(norm(text).split())
        if not heard:
            return True
        recent = [" ".join(norm(t).split()) for t in [self.last_reply_text, *self.speaker.spoken_text[-3:]] if t]
        heard_words = heard.split()
        said_words = set(" ".join(recent).split())
        for r in recent:
            if r and (fuzz.ratio(heard, r) >= 78 or (len(heard) > 12 and fuzz.partial_ratio(heard, r) >= 80)):
                return True
        if len(heard_words) >= 4 and said_words:
            covered = sum(1 for w in heard_words if w in said_words) / len(heard_words)
            if covered >= 0.85:  # nearly every word is one Jarvis just said
                return True
        now = self._clock()
        if heard == self._last_heard and now - self._last_heard_at < 60:
            self._same_count += 1
        else:
            self._same_count = 1
        self._last_heard, self._last_heard_at = heard, now
        return self._same_count >= 3  # the same words three times running is a loop, not a person

    def _note_interruption(self) -> None:
        """Whatever Jarvis was saying is over: store only the spoken part and tell the next turn to move on."""
        history = getattr(self.assistant, "history", None)
        if history is not None and hasattr(history, "close_interrupted"):
            spoken = self.speaker.spoken_text[-1] if self.speaker.spoken_text else ""
            history.close_interrupted(spoken)
        agent = getattr(self.assistant, "agent", None)
        if agent is not None:
            agent.interrupted = True

    def _wake_confirmed(self, heard: str) -> bool:
        """Wake-word detectors fire on TV and chatter. A confident hit is trusted; a weak one only counts when the
        transcript really contains the wake word, so a stray sentence is never run as a command."""
        from jarvis_core.voice.turn import starts_with_wake_word

        a = self.cfg.activation
        strong = max(a.wake_threshold + 0.2, 0.65)
        if self._wake_score >= strong or starts_with_wake_word(heard):
            return True
        # a weak wake-word hit is fine when the voice is clearly yours (the voiceprint is the second opinion)
        return self._speaker_check_on() and (self.last_speaker_score or 0.0) >= 0.65

    async def _wake_is_owner(self) -> bool:
        """A wake-word verifier: with a voiceprint, "Hey Jarvis" only counts when it sounds like you (a TV, a visitor or
        Jarvis's own voice saying the name no longer wakes it)."""
        if self._noisy():
            return True  # the voiceprint is unreliable over loud audio; the transcript must still contain the wake word
        ok = await self._is_owner(self.preroll.tail(1400), threshold=self.cfg.voice.speaker_wake_threshold)
        if not ok:
            log.info("wake word ignored: not your voice")
            if self.wake:
                self.wake.reset()
        return ok

    async def _wake_interrupt(self) -> None:
        if not await self._is_owner(self.preroll.tail(1400), barge=True):
            log.info("wake interrupt ignored: not your voice")
            return
        log.info("interrupted by wake word")
        await self._cancel_turn()
        self._note_interruption()
        await self.frontend.stop_audio()
        await self._begin_listening("wake", preroll_ms=2200)

    async def _barge_in(self) -> None:
        log.info("barge-in")
        await self._cancel_turn()
        self._note_interruption()
        await self.frontend.stop_audio()
        await self._begin_listening("barge", preroll_ms=600)

    # -------------------------------------------------------- hotkey

    async def _hotkey(self, action: str) -> None:
        st = self.sm.state
        if st == State.PAUSED:
            return
        if action == "down":
            self._hotkey_down_at = self._clock()
            if self._muted:
                self._muted = False
                await self.frontend.set_mic_enabled(True)
            if st in (State.IDLE, State.FOLLOW_UP):
                await self._begin_listening("hotkey")
            elif st in (State.SPEAKING, State.THINKING, State.CONFIRMING, State.ANNOUNCING):
                await self._cancel_turn()
                if st in (State.SPEAKING, State.ANNOUNCING):
                    self._note_interruption()
                await self.frontend.stop_audio()
                await self._begin_listening("hotkey")
        elif action == "up":
            held = self._clock() - self._hotkey_down_at
            if self._ptt and st == State.LISTENING and held > 0.45 and self._endpointer:
                await self._end_utterance()
        elif action == "long_press":
            await self.cancel_everything()

    async def cancel_everything(self) -> None:
        if self.sm.state in (State.SPEAKING, State.ANNOUNCING, State.THINKING):
            self._note_interruption()  # the cut-off reply is stored as only what was spoken, so the next turn moves on
        await self._cancel_turn()
        await self.frontend.stop_audio()
        self._endpointer = None
        for h in self.hooks.on_cancel:
            await self._safe(h)
        if self.sm.state not in (State.PAUSED, State.IDLE):
            self.sm.go(State.IDLE)

    # ----------------------------------------------------- announcements

    async def show_preparing(self) -> None:
        """Let the orb appear right away while a briefing is being collected, well before there is anything to say."""
        with contextlib.suppress(Exception):
            await self.frontend.set_state(State.THINKING.value)

    async def narrate(self, text: str) -> None:
        """Say a short progress line from inside a running turn (computer use). Never changes state."""
        if not text or self._muted or self.sm.state in (State.PAUSED, State.IDLE):
            return
        try:
            await self.speaker.speak(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("narration failed", exc_info=True)

    # ------------------------------------------------ promises: never drop something you were told you would hear

    def _outbox_file(self):
        from jarvis_core import paths

        return paths.home() / "outbox.json"

    def _save_outbox(self) -> None:
        try:
            rows = [{k: v for k, v in m.items() if k in ("text", "earcon", "title")} | {"left": max(0.0, m["due"] - self._clock())}
                    for m in self._outbox]
            if rows:
                self._outbox_file().write_text(json.dumps(rows))
            else:
                self._outbox_file().unlink(missing_ok=True)
        except OSError:
            log.debug("could not save the outbox", exc_info=True)

    def restore_outbox(self) -> None:
        """Anything that was waiting to be said when Jarvis last stopped is said now."""
        try:
            rows = json.loads(self._outbox_file().read_text())
        except (OSError, ValueError):
            return
        now = self._clock()
        for r in rows:
            self._outbox.append({"text": r["text"], "earcon": r.get("earcon") or "alert", "title": r.get("title", "Jarvis"),
                                 "kw": {}, "queued": now, "due": now + float(r.get("left", 600)), "notified": False})
        if self._outbox and (self._outbox_task is None or self._outbox_task.done()):
            self._outbox_task = asyncio.create_task(self._drain_outbox(), name="outbox")

    async def announce_reliably(
        self, text: str, *, earcon: str | None = "alert", ttl_s: float = 1800.0, title: str = "Jarvis", **kw: Any
    ) -> bool:
        """Say it now if Jarvis is free; otherwise keep it and say it the moment there is a gap (after the current
        sentence, between your turns, when your call ends or the lid opens). A timer must never be silently dropped."""
        if self.is_free and not self.mic_in_use_by_other and await self.announce(text, earcon=earcon, **kw):
            return True
        log.info("announcement queued (state=%s, capture=%s, mic_in_use_by_other=%s, muted=%s): %r",
                 self.sm.state, self._capture is not None, self.mic_in_use_by_other, self._muted, text)
        now = self._clock()
        self._outbox.append({"text": text, "earcon": earcon, "kw": kw, "title": title, "queued": now, "due": now + ttl_s,
                             "notified": False})
        self._save_outbox()
        if self._outbox_task is None or self._outbox_task.done():
            self._outbox_task = asyncio.create_task(self._drain_outbox(), name="outbox")
        return True  # accepted: it will be delivered

    async def _drain_outbox(self) -> None:
        while self._outbox:
            now = self._clock()
            self._outbox = [m for m in self._outbox if m["due"] > now]
            for m in self._outbox:  # busy for a few seconds: make sure you can still see it
                if not m["notified"] and now - m["queued"] >= 3.0:
                    m["notified"] = True
                    with contextlib.suppress(Exception):
                        await self.frontend.notify(m["title"], m["text"])
            if self._outbox and self.is_free and not self.mic_in_use_by_other:
                m = self._outbox[0]
                lead = "Pardon the delay, sir. " if now - m["queued"] > 8 else ""
                if await self.announce(lead + m["text"], earcon=m["earcon"], **m["kw"]):
                    self._outbox.pop(0)
                    self._save_outbox()
                    continue
            await asyncio.sleep(self._outbox_poll_s)
        self._save_outbox()

    async def announce(
        self,
        text: str | AsyncIterator[str],
        *,
        earcon: str | None = None,
        remember: bool = True,
        follow_up: bool = True,
    ) -> bool:
        """Speak proactively. Returns False if Jarvis is busy or paused."""
        if not self.is_free:
            return False
        self.sm.go(State.ANNOUNCING)

        async def _say() -> str:
            if earcon:
                await self.frontend.earcon(earcon)
            if isinstance(text, str):
                return await self.speaker.speak(text)
            return await self.speaker.speak_stream(text)

        self._announce_task = asyncio.create_task(_say(), name="announcement")
        try:
            spoken = await self._announce_task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if me is not None and getattr(me, "cancelling", lambda: 0)():
                raise  # the caller itself is being cancelled
            return False  # the user interrupted the announcement: it is over
        except Exception:
            log.exception("announcement failed")
            spoken = ""
        finally:
            self._announce_task = None
        if remember and spoken and hasattr(self.assistant, "history"):
            self.assistant.history.add({"role": "assistant", "content": spoken})
        if self.sm.state == State.ANNOUNCING:
            if follow_up:
                self.sm.go(State.FOLLOW_UP)
                await self._begin_listening("followup", followup=True)
            else:
                self.sm.go(State.IDLE)
        return True

    # ------------------------------------------------- pause / resume

    async def _welcome_back(self, paused_at: datetime | None) -> None:
        """A short greeting on return, unless a brief already took the moment or you were only away for a flicker."""
        a = self.cfg.activation
        if not a.welcome_back or self._muted or self.mic_in_use_by_other:
            return
        away = (datetime.now().astimezone() - paused_at).total_seconds() if paused_at else 0.0
        now = self._clock()
        if away < a.welcome_back_min_away_s or now - self._last_welcome < a.welcome_back_cooldown_min * 60:
            return
        self._last_welcome = now
        line = welcome_line(self.cfg, datetime.now().astimezone(), first_of_day=self._first_welcome_of_day())
        await self.announce(line, remember=False, follow_up=False)

    def _first_welcome_of_day(self) -> bool:
        """True the first time this (briefing) day that Jarvis greets you, i.e. the first lid-open after waking up."""
        day = briefing_day(datetime.now().astimezone(), self.cfg.briefing.day_rollover_hour)
        first = self._welcome_day != day
        self._welcome_day = day
        return first

    async def _lifecycle(self, event: str) -> None:
        if event in ("will_sleep", "screens_slept"):
            self._asleep = True
        elif event == "screen_locked":
            self._locked = True
        elif event in ("did_wake", "screens_woke"):
            self._asleep = False
        elif event == "screen_unlocked":
            self._locked = False
        if self._asleep or self._locked:
            await self.pause()
        else:
            await self.resume()

    async def pause(self) -> None:
        if self.sm.state == State.PAUSED:
            return
        log.info("pausing")
        self.paused_at = datetime.now().astimezone()
        await self._cancel_turn()
        await self.frontend.stop_audio()
        await self.frontend.set_mic_enabled(False)
        self._endpointer = None
        self._utt = bytearray()
        self.preroll.clear()
        while not self._q.empty():
            self._q.get_nowait()
        if self.sm.state != State.BOOTING:
            self.sm.go(State.PAUSED)
        for h in self.hooks.on_pause:
            await self._safe(h)

    async def resume(self) -> None:
        if self.sm.state != State.PAUSED:
            return
        log.info("resuming")
        self.sm.go(State.RESUMING)
        await self.frontend.set_mic_enabled(not self._muted)
        paused_at, self.paused_at = self.paused_at, None
        for h in self.hooks.on_resume:
            await self._safe(h, paused_at)
        if self.sm.state == State.RESUMING:
            self.sm.go(State.IDLE)
            await self._welcome_back(paused_at)
        else:  # the morning brief (with its own greeting) took this moment: today's first welcome is spent
            self._first_welcome_of_day()
