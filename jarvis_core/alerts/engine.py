"""Polls the alert rules, queues what is new, and speaks it when the user is free."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from jarvis_core.alerts.models import Alert
from jarvis_core.alerts.rules import Rule, default_rules, meeting_speech
from jarvis_core.briefing.env import Env
from jarvis_core.briefing.store import Store
from jarvis_core.config import Config
from jarvis_core.policy.presence import Presence
from jarvis_core.policy.queue import AlertQueue
from jarvis_core.policy.quiet import in_quiet_hours

log = logging.getLogger(__name__)

TICK_S = 20.0
BATCH = 3
RULE_TIMEOUT_S = 25.0


def render_speech(a: Alert, now: float) -> str:
    """Time-sensitive alerts are re-rendered at delivery so 'in 10 minutes' stays true."""
    if a.kind == "meeting" and "start" in a.payload:
        return meeting_speech(a.payload.get("title", "your meeting"), a.payload["start"], now, a.payload.get("location", ""))
    return a.speech


def compose_batch(alerts: list[Alert], now: float, *, held: bool) -> str:
    if len(alerts) == 1:
        return render_speech(alerts[0], now)
    lead = "Sir, a few things came up while you were busy." if held else "Sir, a few things."
    return " ".join([lead, *(render_speech(a, now) for a in alerts)])


class AlertEngine:
    def __init__(
        self,
        cfg: Config,
        env: Env,
        queue: AlertQueue,
        presence: Presence,
        *,
        announce: Callable[..., Awaitable[bool]],
        is_free: Callable[[], bool],
        orb_update: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
        rules: list[Rule] | None = None,
        gate: Callable[[], bool] | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.env = env
        self.store: Store = env.store
        self.queue = queue
        self.presence = presence
        self._announce = announce
        self._is_free = is_free
        self._orb = orb_update
        self.rules = rules if rules is not None else default_rules()
        self._gate = gate or (lambda: True)
        self._now = now or env.now
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._awake = True
        self._flushing = False
        self._last_poll = -1e9
        self._held_since: float | None = None
        self.last_offer: dict[str, Any] | None = None

    # ------------------------------------------------------------------ hooks

    async def on_ready(self) -> None:
        self._awake = True
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="alert-engine")
        await self._sync_orb()

    async def on_pause(self) -> None:
        self._awake = False

    async def on_resume(self, paused_at: datetime | None = None) -> None:
        self._awake = True
        self._last_poll = -1e9  # poll straight away after wake
        await self.tick()

    async def on_idle(self) -> None:
        await self.flush()

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    # ------------------------------------------------------------------- logic

    def _throttled(self, kind: str) -> bool:
        minutes = self.cfg.alerts.cooldown_minutes.get(kind)
        if not minutes:
            return False
        fired = self.store.alert_fired_at(f"kind:{kind}")
        return fired is not None and self._clock() - fired < minutes * 60

    def _accept(self, a: Alert) -> bool:
        if self.store.alert_fired_at(a.key) is not None:
            return False
        if self._throttled(a.kind):
            return False
        return True

    def _queued(self, a: Alert) -> None:
        self.store.alert_mark(a.key, self._clock())
        self.store.alert_mark(f"kind:{a.kind}", self._clock())
        ids = a.payload.get("seen_ids")
        if ids:
            self.store.mark_seen(a.payload.get("seen_kind", a.kind), list(ids))

    async def _run_rule(self, rule: Rule) -> list[Alert]:
        try:
            return await asyncio.wait_for(rule.check(self.env), RULE_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("alert rule %s failed: %r", rule.kind, e)
            return []

    async def poll(self) -> list[Alert]:
        """Run every rule once and queue new alerts. Returns what was queued."""
        results = await asyncio.gather(*(self._run_rule(r) for r in self.rules))
        added: list[Alert] = []
        for alerts in results:
            for a in alerts:
                if a.expired(self._clock()) or not self._accept(a):
                    continue
                if self.queue.add(a):
                    self._queued(a)
                    added.append(a)
                    if self._held_since is None:
                        self._held_since = self._clock()
        self._last_poll = self._clock()
        if added:
            log.info("queued alerts: %s", [a.key for a in added])
        return added

    def _can_deliver_now(self) -> bool:
        if in_quiet_hours(self.cfg.user.quiet_hours, self._now()):
            return False
        return self.presence.can_speak() and self._gate()

    async def flush(self, force: bool = False) -> bool:
        """Speak queued alerts if the user is free. ``force`` (orb tap) ignores Focus/call/quiet hours."""
        if self._flushing or not self._awake:
            return False
        await self._sync_orb()
        if not len(self.queue):
            self._held_since = None
            return False
        if not force and not self._can_deliver_now():
            return False
        if not self._is_free():
            return False
        self._flushing = True
        batch: list[Alert] = []
        try:
            batch = self.queue.take(BATCH)
            if not batch:
                return False
            held = self._held_since is not None and self._clock() - self._held_since > 120
            text = compose_batch(batch, self._clock(), held=held)
            ok = await self._announce(text, earcon="alert", follow_up=any(a.follow_up for a in batch))
            if not ok:
                self.queue.restore(batch)
                return False
            offers = [a.payload["offer"] for a in batch if "offer" in a.payload]
            if offers:
                policy = self.env.services.get("offers")
                if policy is not None:
                    policy.mark_offered(offers[0].get("thread_id", ""))
                self.last_offer = offers[0]
                if self.env.ctx is not None:
                    self.env.ctx.state["alert_offer"] = offers[0]
            if not len(self.queue):
                self._held_since = None
            return True
        except asyncio.CancelledError:
            self.queue.restore(batch)
            raise
        except Exception:
            log.exception("alert delivery failed")
            self.queue.restore(batch)
            return False
        finally:
            self._flushing = False
            await self._sync_orb()

    async def tick(self) -> None:
        if not self._awake:
            return
        if self._clock() - self._last_poll >= self.cfg.alerts.poll_seconds:
            await self.poll()
        await self.flush()

    async def _sync_orb(self) -> None:
        if self._orb is None:
            return
        try:
            await self._orb(self.queue.orb_items())
        except Exception:
            log.debug("orb queue update failed", exc_info=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("alert tick failed")

    # ------------------------------------------------------------- user-facing

    def summary(self) -> str:
        pending = self.queue.pending()
        if not pending:
            return "Nothing outstanding, sir."
        now = self._clock()
        return compose_batch(pending[:8], now, held=False)

    def dismiss_all(self) -> int:
        return self.queue.clear()
