"""Held-alert queue: persisted, de-duplicated, prioritised, mirrored to the orb."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from jarvis_core.alerts.models import PRIORITY_ORDER, Alert
from jarvis_core.briefing.store import Store

log = logging.getLogger(__name__)

SNAPSHOT = "alert_queue"
MAX_QUEUE = 25


class AlertQueue:
    def __init__(self, store: Store | None = None, clock: Callable[[], float] = time.time) -> None:
        self._store = store
        self._clock = clock
        self._items: dict[str, Alert] = {}
        self._load()

    # -- persistence ---------------------------------------------------------------
    def _load(self) -> None:
        if not self._store:
            return
        snap = self._store.snapshot_get(SNAPSHOT) or {}
        for d in snap.get("items", []):
            try:
                a = Alert.from_json(d)
            except TypeError:
                continue
            if not a.expired(self._clock()):
                self._items[a.key] = a

    def _save(self) -> None:
        if self._store:
            self._store.snapshot_put(SNAPSHOT, {"items": [a.to_json() for a in self._items.values()]})

    # -- operations -------------------------------------------------------------------
    def add(self, alert: Alert) -> bool:
        """Queue an alert. Returns False if one with the same key is already waiting."""
        self.prune()
        if alert.key in self._items:
            return False
        self._items[alert.key] = alert
        if len(self._items) > MAX_QUEUE:
            worst = max(self._items.values(), key=lambda a: (PRIORITY_ORDER[a.priority], -a.created))
            self._items.pop(worst.key, None)
        self._save()
        return True

    def prune(self) -> list[Alert]:
        now = self._clock()
        gone = [a for a in self._items.values() if a.expired(now)]
        for a in gone:
            self._items.pop(a.key, None)
        if gone:
            self._save()
        return gone

    def pending(self) -> list[Alert]:
        self.prune()
        return sorted(self._items.values(), key=lambda a: (PRIORITY_ORDER[a.priority], a.created))

    def take(self, limit: int = 3) -> list[Alert]:
        """Remove and return the most important alerts (for delivery)."""
        out = self.pending()[:limit]
        for a in out:
            self._items.pop(a.key, None)
        if out:
            self._save()
        return out

    def restore(self, alerts: list[Alert]) -> None:
        """Put alerts back (delivery didn't happen)."""
        for a in alerts:
            self._items.setdefault(a.key, a)
        self._save()

    def clear(self) -> int:
        n = len(self._items)
        self._items.clear()
        self._save()
        return n

    def __len__(self) -> int:
        self.prune()
        return len(self._items)

    def orb_items(self) -> list[dict[str, str]]:
        return [{"id": a.key, "title": a.title, "priority": a.priority} for a in self.pending()]
