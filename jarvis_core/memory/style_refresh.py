"""Rebuild the writing-style cards from your sent messages: once on first run, then weekly."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from jarvis_core import paths
from jarvis_core.memory.style import learn_style

log = logging.getLogger(__name__)

WEEK_S = 7 * 86400
SAMPLES = 200


class StyleRefresher:
    def __init__(self, hub, memory, *, clock=time.time, interval_s: float = WEEK_S) -> None:
        self.hub = hub
        self.memory = memory
        self._clock = clock
        self.interval_s = interval_s
        self._running = False

    def _file(self):
        return paths.home() / "style_refresh.json"

    def due(self) -> bool:
        try:
            last = float(json.loads(self._file().read_text()).get("last", 0))
        except (OSError, ValueError, TypeError):
            last = 0.0
        return self._clock() - last >= self.interval_s

    async def refresh(self) -> dict[str, bool]:
        """Learn one style card per messaging app from up to 200 of your sent messages. Returns {app: learned}."""
        out: dict[str, bool] = {}
        for app, reader in (getattr(self.hub, "readers", None) or {}).items():
            getter = getattr(reader, "all_sent", None)
            if getter is None:
                continue
            try:
                samples = await asyncio.to_thread(getter, SAMPLES)
            except Exception:  # noqa: BLE001 - one unreadable source must not stop the others
                log.info("could not read sent messages from %s", app, exc_info=True)
                continue
            out[app] = learn_style(self.memory, app, samples)
        try:
            self._file().write_text(json.dumps({"last": self._clock(), "apps": out}))
        except OSError:
            log.warning("could not record the style refresh", exc_info=True)
        return out

    async def on_idle(self) -> None:
        if self._running or not self.due():
            return
        self._running = True
        try:
            res = await self.refresh()
            log.info("writing style refreshed: %s", res)
        finally:
            self._running = False
