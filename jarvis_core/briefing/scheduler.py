"""Decides *when* to brief: first open of the day, custom time slots, missed slots on wake."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta

from jarvis_core.briefing.service import BriefingService
from jarvis_core.briefing.store import Store, briefing_day
from jarvis_core.config import Config, parse_hhmm
from jarvis_core.policy.quiet import in_quiet_hours

log = logging.getLogger(__name__)

FIRST_OPEN = "first_open"
TICK_S = 20.0
ON_TIME_WINDOW = timedelta(minutes=90)  # later than this a slot counts as "missed"


class BriefingScheduler:
    def __init__(
        self,
        cfg: Config,
        service: BriefingService,
        store: Store,
        *,
        announce: Callable[..., Awaitable[bool]],
        is_free: Callable[[], bool],
        gate: Callable[[], bool] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.cfg = cfg
        self.service = service
        self.store = store
        self._announce = announce
        self._is_free = is_free
        self._gate = gate or (lambda: True)
        self._now = now
        self._task: asyncio.Task | None = None
        self._awake = True
        self._busy = False

    # ------------------------------------------------------------------ hooks

    async def on_ready(self) -> None:
        self._awake = True
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="briefing-scheduler")
        await self.tick()

    async def on_pause(self) -> None:
        self._awake = False

    async def on_resume(self, paused_at: datetime | None = None) -> None:
        self._awake = True
        await self.tick()

    async def on_idle(self) -> None:
        await self.tick()

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ------------------------------------------------------------------- logic

    def _day(self, now: datetime) -> date:
        return briefing_day(now, self.cfg.briefing.day_rollover_hour)

    def _slot_time(self, now: datetime, hhmm: str) -> datetime:
        h, m = parse_hhmm(hhmm)
        base = self._day(now)
        t = datetime(base.year, base.month, base.day, h, m, tzinfo=now.tzinfo)
        # Slots before the rollover hour (e.g. 01:00 with rollover 04:00) belong to the next calendar day.
        if h < self.cfg.briefing.day_rollover_hour:
            t += timedelta(days=1)
        return t

    def pending_slots(self, now: datetime) -> list[str]:
        day = self._day(now)
        return [s for s in self.cfg.briefing.slots if self._slot_time(now, s) <= now and not self.store.delivered(day, s)]

    def decide(self, now: datetime) -> tuple[str, str, list[str]] | None:
        """What to deliver right now: (mode, slot_to_mark, extra_slots_to_mark) or None."""
        b = self.cfg.briefing
        day = self._day(now)
        pending = self.pending_slots(now)
        if b.first_open_full_brief and not self.store.delivered(day, FIRST_OPEN):
            return "full", FIRST_OPEN, pending  # a full brief also covers any slots already past
        if not pending:
            return None
        newest = max(pending, key=lambda s: self._slot_time(now, s))
        late = now - self._slot_time(now, newest) > ON_TIME_WINDOW
        if late and not b.deliver_missed_slots_on_wake:
            for s in pending:
                self.store.mark_delivered(day, s, "skipped")
            return None
        return "delta", newest, [s for s in pending if s != newest]

    async def tick(self) -> bool:
        if self._busy or not self._awake:
            return False
        now = self._now()
        if in_quiet_hours(self.cfg.user.quiet_hours, now):
            return False
        plan = self.decide(now)
        if plan is None:
            return False
        if not self._is_free() or not self._gate():
            return False  # try again on the next tick / when Jarvis goes idle
        mode, slot, covered = plan
        self._busy = True
        try:
            log.info("delivering %s briefing (slot %s)", mode, slot)
            ok = await self.service.deliver(self._announce, mode=mode, day=self._day(now), slot=slot)
            if ok:
                for s in covered:
                    self.store.mark_delivered(self._day(now), s, "covered")
            return ok
        except Exception:
            log.exception("briefing delivery failed")
            return False
        finally:
            self._busy = False

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
