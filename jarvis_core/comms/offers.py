"""Proactive reply offers: at most one per cooldown window, and 'not now' snoozes a thread."""

from __future__ import annotations

import time
from collections.abc import Callable

from jarvis_core.briefing.store import Store
from jarvis_core.config import MessagingCfg


class OfferPolicy:
    def __init__(self, store: Store, cfg: MessagingCfg, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.cfg = cfg
        self._clock = clock

    def allowed(self, thread_id: str) -> bool:
        if not self.cfg.proactive_reply_offers:
            return False
        until = self.store.alert_fired_at(f"offer:snooze:{thread_id}")
        if until is not None and until > self._clock():
            return False
        last = self.store.alert_fired_at("offer:last")
        return last is None or self._clock() - last >= self.cfg.offer_cooldown_minutes * 60

    def mark_offered(self, thread_id: str = "") -> None:
        self.store.alert_mark("offer:last", self._clock())

    def snooze(self, thread_id: str) -> None:
        self.store.alert_mark(f"offer:snooze:{thread_id}", self._clock() + self.cfg.offer_snooze_hours * 3600)
