"""Runtime assembly: builds every subsystem and connects it to the daemon."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from jarvis_core.assistant import Assistant
from jarvis_core.brain.llm import build_llm
from jarvis_core.config import Config
from jarvis_core.daemon import Daemon, Hooks
from jarvis_core.frontend import Frontend
from jarvis_core.voice.stt import build_stt
from jarvis_core.voice.tts import build_tts
from jarvis_core.voice.vad import build_vad
from jarvis_core.voice.wake import build_wake

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    cfg: Config
    daemon: Daemon
    frontend: Frontend
    assistant: Assistant
    extras: dict[str, Any] = field(default_factory=dict)
    closers: list[Callable[[], Awaitable[None] | None]] = field(default_factory=list)

    async def run(self) -> None:
        try:
            await self.daemon.run()
        finally:
            for c in reversed(self.closers):
                try:
                    res = c()
                    if hasattr(res, "__await__"):
                        await res
                except Exception:
                    log.exception("shutdown hook failed")


async def build_runtime(cfg: Config, *, standalone: bool, ipc_file: str | None) -> Runtime:
    if standalone:
        from jarvis_core.standalone import StandaloneFrontend

        frontend: Frontend = StandaloneFrontend()
    else:
        from jarvis_core.ipc.frontend import IPCFrontend

        frontend = IPCFrontend(ipc_file)

    llm = build_llm(cfg)
    assistant = Assistant.create(cfg, llm=llm, frontend=frontend)
    stt = build_stt(cfg.voice)
    tts = build_tts(cfg.voice)
    wake = build_wake(cfg.activation.wake_word, cfg.activation.wake_threshold)
    hooks = Hooks()
    daemon = Daemon(cfg, frontend, assistant=assistant, stt=stt, tts=tts, wake=wake,
                    vad_factory=build_vad, hooks=hooks)
    if cfg.voice.speaker_verify:
        try:
            from jarvis_core.voice.speaker_id import SpeakerVerifier

            daemon.verifier = SpeakerVerifier()
            log.info("voice recognition: %s", "enrolled" if daemon.verifier.enrolled else "ready (not enrolled yet)")
        except Exception as e:  # noqa: BLE001
            log.info("voice recognition unavailable: %s", e)
    if assistant.ctx is not None:
        assistant.ctx.services["daemon"] = daemon
        from jarvis_core.skills.productivity import restore_timers

        n = restore_timers(assistant.ctx)
        if n:
            log.info("restored %d timer(s) from the last run", n)
        assistant.ctx.announce = daemon.announce_reliably  # timers and reminders must always get through
        if cfg.llm.announce_switches and hasattr(llm, "set_notify"):
            _pending: set[asyncio.Task] = set()

            def _say_switch(text: str) -> None:
                t = asyncio.get_running_loop().create_task(daemon.announce_reliably(text, earcon=None, ttl_s=120.0, remember=False, follow_up=False))
                _pending.add(t)
                t.add_done_callback(_pending.discard)

            llm.set_notify(_say_switch)
        assistant.ctx.narrate = daemon.narrate
    if hasattr(frontend, "on_shell_lost"):
        frontend.on_shell_lost = daemon.request_stop  # type: ignore[attr-defined]
    rt = Runtime(cfg, daemon, frontend, assistant)
    _attach_housekeeping(rt)
    _attach_access_report(rt)
    _attach_memory(rt, llm)
    _attach_briefing(rt, llm)
    _attach_comms(rt, llm)
    _attach_vision(rt, llm)
    _attach_alerts(rt)
    return rt


def _attach_briefing(rt: Runtime, llm) -> None:
    from jarvis_core.briefing.env import Env
    from jarvis_core.briefing.scheduler import BriefingScheduler
    from jarvis_core.briefing.service import BriefingService
    from jarvis_core.briefing.store import Store

    ctx = rt.assistant.ctx
    store = Store()
    env = Env(cfg=rt.cfg, http=Env.make_client(), store=store, ctx=ctx, services=ctx.services if ctx else {})
    service = BriefingService(rt.cfg, env, llm)
    if ctx is not None:
        ctx.services["briefing"] = service
        ctx.services["store"] = store
    d = rt.daemon
    sched = BriefingScheduler(
        rt.cfg, service, store, announce=d.announce, is_free=lambda: d.is_free,
        gate=lambda: rt.extras.get("gate", lambda: True)(), on_start=d.show_preparing,
    )
    d.hooks.on_ready.append(sched.on_ready)
    d.hooks.on_resume.append(sched.on_resume)
    d.hooks.on_pause.append(sched.on_pause)
    d.hooks.on_idle.append(sched.on_idle)

    async def brief_now() -> None:
        await d.show_preparing()
        await service.deliver(d.announce, mode="full", day=sched._day(sched._now()), slot=f"manual-{int(sched._now().timestamp())}")

    d.hooks.on_command["brief_now"] = brief_now
    rt.extras.update(briefing=service, scheduler=sched, store=store, env=env)
    rt.closers.append(env.http.aclose)
    rt.closers.append(sched.stop)


def _attach_alerts(rt: Runtime) -> None:
    from jarvis_core.alerts.engine import AlertEngine
    from jarvis_core.policy.presence import Presence
    from jarvis_core.policy.queue import AlertQueue

    ctx = rt.assistant.ctx
    d = rt.daemon
    store, env = rt.extras["store"], rt.extras["env"]
    presence = Presence()
    queue = AlertQueue(store)
    engine = AlertEngine(
        rt.cfg, env, queue, presence,
        announce=d.announce, is_free=lambda: d.is_free, orb_update=d.frontend.orb_queue,
    )
    d.hooks.on_device_usage.append(presence.on_device_usage)
    d.hooks.on_ready.append(engine.on_ready)
    d.hooks.on_resume.append(engine.on_resume)
    d.hooks.on_pause.append(engine.on_pause)
    d.hooks.on_idle.append(engine.on_idle)

    async def open_queue() -> None:
        await engine.flush(force=True)

    d.hooks.on_command["open_queue"] = open_queue
    rt.extras["gate"] = presence.can_speak  # briefings wait out Focus / calls too
    rt.extras.update(alerts=engine, presence=presence, alert_queue=queue)
    if ctx is not None:
        ctx.services["alerts"] = engine
        ctx.services["presence"] = presence
    rt.closers.append(engine.stop)


def _attach_comms(rt: Runtime, llm) -> None:
    from jarvis_core.comms.setup import ContactRefresher, build_comms

    ctx = rt.assistant.ctx
    if ctx is None:
        return
    hub = build_comms(rt.cfg, ctx, rt.extras["store"], llm)
    refresher = ContactRefresher(hub)
    rt.daemon.hooks.on_ready.append(refresher.on_ready)
    rt.closers.append(refresher.stop)
    rt.extras["comms"] = hub
    mem = rt.extras.get("memory")  # memory is attached first
    if mem is not None:
        from jarvis_core.memory.style_refresh import StyleRefresher

        rt.daemon.hooks.on_idle.append(StyleRefresher(hub, mem).on_idle)  # first run, then weekly


def _attach_vision(rt: Runtime, llm) -> None:
    from jarvis_core.vision.computer_use import ComputerUse
    from jarvis_core.vision.screen import ScreenVision

    ctx = rt.assistant.ctx
    if ctx is None:
        return
    d = rt.daemon
    vision = ScreenVision(ctx, llm)
    cu = ComputerUse(ctx, llm, vision, set_active=lambda active: setattr(d, "cu_active", active))
    d.hooks.on_cancel.append(cu.on_cancel)  # voice "stop", hotkey long-press, mouse-move kill switch
    ctx.services["vision"] = vision
    ctx.services["computer_use"] = cu
    rt.extras.update(vision=vision, computer_use=cu)


def _attach_memory(rt: Runtime, llm) -> None:
    from jarvis_core.memory.extractor import Extractor
    from jarvis_core.memory.store import SQLiteMemory

    ctx = rt.assistant.ctx
    if ctx is None or not rt.cfg.memory.enabled:
        return
    mem = SQLiteMemory()
    ctx.services["memory"] = mem
    extractor = Extractor(mem, llm, enabled=rt.cfg.memory.auto_extract)
    rt.daemon.hooks.on_turn_done.append(extractor.on_turn_done)
    from jarvis_core.memory.extractor import EpisodeRecorder

    rt.daemon.hooks.on_turn_done.append(EpisodeRecorder(mem, enabled=rt.cfg.memory.episodes).on_turn_done)
    rt.extras.update(memory=mem, extractor=extractor)
    rt.closers.append(mem.close)


def _attach_access_report(rt: Runtime) -> None:
    """Every few minutes, record what this daemon process can read (Calendar, Messages, WhatsApp, Focus) for ``jarvisd doctor``."""
    import asyncio

    from jarvis_core import doctor

    async def loop() -> None:
        while True:
            try:
                await asyncio.get_running_loop().run_in_executor(None, doctor.write_access_report, rt.cfg)
            except Exception:  # noqa: BLE001 - a diagnostic must never take Jarvis down
                log.debug("access report failed", exc_info=True)
            await asyncio.sleep(300)

    async def on_ready() -> None:
        rt.extras["access_task"] = asyncio.create_task(loop(), name="access-report")

    rt.daemon.hooks.on_ready.append(on_ready)


def _attach_housekeeping(rt: Runtime) -> None:
    from jarvis_core.housekeeping import TranscriptLog

    log_ = TranscriptLog(rt.cfg.safety.transcript_retention_days)
    rt.daemon.hooks.on_ready.append(log_.on_ready)
    rt.daemon.hooks.on_turn_done.append(log_.on_turn_done)
    rt.extras["transcripts"] = log_
