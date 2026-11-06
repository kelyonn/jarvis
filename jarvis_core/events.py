"""Tiny in-process async pub/sub bus."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None] | None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        self._subs[topic].append(handler)

        def unsubscribe() -> None:
            if handler in self._subs[topic]:
                self._subs[topic].remove(handler)

        return unsubscribe

    async def publish(self, topic: str, payload: Any = None) -> None:
        for h in list(self._subs.get(topic, ())):
            try:
                res = h(payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # a bad subscriber must never break the bus
                log.exception("event handler failed for %s", topic)

    def publish_nowait(self, topic: str, payload: Any = None) -> None:
        loop = asyncio.get_event_loop()
        loop.create_task(self.publish(topic, payload))
