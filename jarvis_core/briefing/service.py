"""BriefingService: collect -> (delta) -> compose -> deliver. Also fast single-topic answers."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime
from typing import Any

from jarvis_core.briefing import composer
from jarvis_core.briefing.collectors import calendar as cal_c
from jarvis_core.briefing.collectors import comms as comms_c
from jarvis_core.briefing.collectors import markets as mk_c
from jarvis_core.briefing.collectors import news as news_c
from jarvis_core.briefing.collectors import reminders as rem_c
from jarvis_core.briefing.collectors import weather as wx_c
from jarvis_core.briefing.env import Env, gather_limited
from jarvis_core.briefing.models import BriefingData, MailItem, SectionResult
from jarvis_core.briefing.store import Store, briefing_day
from jarvis_core.config import Config
from jarvis_core.interfaces import LLM

log = logging.getLogger(__name__)

SNAPSHOT = "last_brief"
COLLECTOR_TIMEOUT_S = 9.0


def _hash(s: str) -> str:
    return hashlib.sha1(s.lower().encode()).hexdigest()[:16]


class BriefingService:
    def __init__(self, cfg: Config, env: Env, llm: LLM | None) -> None:
        self.cfg = cfg
        self.env = env
        self.llm = llm
        self.store: Store = env.store

    # ------------------------------------------------------------------ collect

    def _collector(self, key: str, mode: str, since: datetime | None):
        env = self.env
        if key == "weather":
            return wx_c.collect(env)
        if key == "calendar":
            return cal_c.collect(env, only_upcoming=(mode != "full"))
        if key == "email":
            return comms_c.collect(env, "email", since)
        if key == "messages":
            return comms_c.collect(env, "messages", since)
        if key == "markets":
            return mk_c.collect(env)
        if key.startswith("news_"):
            return news_c.collect(env, key.removeprefix("news_"))
        if key == "reminders":
            return rem_c.collect(env)
        if key == "tomorrow":
            from jarvis_core.briefing.collectors import tomorrow as tm_c

            return tm_c.collect(env)
        return None

    async def collect(self, mode: str = "full", since: datetime | None = None, sections: list[str] | None = None) -> BriefingData:
        now = self.env.now()
        keys = [k for k in (sections or self.cfg.briefing.sections) if k != "greeting"]
        if sections is None and now.hour >= self.cfg.briefing.evening_hour:
            keys.append("tomorrow")  # the evening update also looks ahead
        coros = {}
        for k in keys:
            c = self._collector(k, mode, since)
            if c is not None:
                coros[k] = c
        results = await gather_limited(coros, COLLECTOR_TIMEOUT_S)
        data = BriefingData(generated_at=now, mode=mode, since=since)
        for k in keys:
            r = results.get(k)
            if k not in results:
                continue
            if isinstance(r, BaseException):
                log.warning("briefing section %s failed: %s", k, r or type(r).__name__)
                data.sections[k] = SectionResult(k, ok=False, error=str(r) or type(r).__name__)
            else:
                data.sections[k] = SectionResult(k, data=r)
        return data

    # -------------------------------------------------------------------- delta

    def apply_delta(self, data: BriefingData) -> BriefingData:
        """Keep only what changed since the previous brief."""
        snap = self.store.snapshot_get(SNAPSHOT) or {}
        for key, res in list(data.sections.items()):
            if not res.ok:
                if key not in composer.CRITICAL:
                    del data.sections[key]
                continue
            if key == "weather":
                prev = snap.get("weather")
                w = res.data
                if prev and abs(w.temp_c - prev["temp"]) < 4 and abs(w.rain_chance_pct - prev["rain"]) < 25 and w.condition == prev["cond"]:
                    del data.sections[key]
            elif key == "markets":
                prev_prices = snap.get("markets", {})
                moved = []
                for q in res.data:
                    p = prev_prices.get(q.symbol)
                    if p is None or abs(q.price - p) / p * 100 >= 0.4:
                        moved.append(q)
                if moved and prev_prices:
                    res.data = moved
                elif prev_prices:
                    del data.sections[key]
            elif key.startswith("news_"):
                fresh = [h for h in res.data if not self.store.seen("news", _hash(h.title))][:2]
                if fresh:
                    res.data = fresh
                else:
                    del data.sections[key]
            elif key in ("email", "messages"):
                fresh = [m for m in res.data if not self.store.seen("mail", _hash(m.id))]
                if fresh:
                    res.data = fresh
                else:
                    del data.sections[key]
            elif key == "calendar":
                res.data = [e for e in res.data if e.start >= data.generated_at][:4]
                if not res.data:
                    del data.sections[key]
            elif key == "reminders":
                if not res.data:
                    del data.sections[key]
        return data

    def _remember(self, data: BriefingData) -> None:
        """Persist what was just reported so the next delta only covers changes."""
        snap: dict[str, Any] = {"at": data.generated_at.isoformat()}
        prev = self.store.snapshot_get(SNAPSHOT) or {}
        w = data.get("weather")
        if w:
            snap["weather"] = {"temp": w.temp_c, "rain": w.rain_chance_pct, "cond": w.condition}
        elif "weather" in prev:
            snap["weather"] = prev["weather"]
        m = data.get("markets")
        snap["markets"] = {q.symbol: q.price for q in m} if m else prev.get("markets", {})
        for key in ("news_world", "news_india", "news_tech"):
            for h in data.get(key) or []:
                self.store.mark_seen("news", [_hash(h.title)])
        for key in ("email", "messages"):
            items: list[MailItem] = data.get(key) or []
            self.store.mark_seen("mail", [_hash(i.id) for i in items])
        self.store.snapshot_put(SNAPSHOT, snap)

    def last_brief_at(self) -> datetime | None:
        snap = self.store.snapshot_get(SNAPSHOT)
        if not snap or "at" not in snap:
            return None
        return datetime.fromisoformat(snap["at"])

    # --------------------------------------------------------------------- build

    async def build(self, mode: str = "auto") -> BriefingData:
        last = self.last_brief_at()
        today = briefing_day(self.env.now(), self.cfg.briefing.day_rollover_hour)
        if mode == "auto":
            same_day = last is not None and briefing_day(last, self.cfg.briefing.day_rollover_hour) == today
            mode = "delta" if same_day else "full"
        if mode == "delta" and last is None:
            mode = "full"
        since = last if mode == "delta" else None
        data = await self.collect(mode, since)
        if mode == "delta":
            self.apply_delta(data)
        return data

    def reply_offer(self, data: BriefingData) -> str:
        """A sign-off offer to draft a reply to the top VIP message or email, if offers are allowed right now."""
        policy = self.env.services.get("offers")
        if policy is None or not self.cfg.messaging.proactive_reply_offers:
            return ""
        top: MailItem | None = None
        for key in ("messages", "email"):
            for m in data.get(key) or []:
                if m.vip and ":" in m.id:
                    top = top or m
        if top is None or not policy.allowed(top.id):
            return ""
        source, item_id = top.id.split(":", 1)
        if self.env.ctx is not None:
            self.env.ctx.state["alert_offer"] = {"type": "reply", "source": source, "item_id": item_id,
                                                 "sender": top.sender, "thread_id": top.id}
        policy.mark_offered(top.id)
        return f"Shall I draft a reply to {top.sender}?"

    async def stream(self, data: BriefingData) -> AsyncIterator[str]:
        async for chunk in composer.compose_stream(self.llm, self.cfg, data):
            yield chunk
        offer = self.reply_offer(data)
        if offer:
            yield " " + offer

    async def text(self, mode: str = "auto") -> str:
        data = await self.build(mode)
        out = await composer.compose_text(self.llm, self.cfg, data)
        self._remember(data)
        return out

    async def deliver(
        self,
        announce: Callable[..., Awaitable[bool]],
        *,
        mode: str,
        day: date,
        slot: str,
    ) -> bool:
        """Collect, compose and speak. Marks the slot delivered only if the announcement actually played."""
        data = await self.build(mode)
        ok = await announce(self.stream(data), earcon="chime", follow_up=True)
        if ok:
            self._remember(data)
            self.store.mark_delivered(day, slot, data.mode)
        return bool(ok)

    # --------------------------------------------------------------- quick answers

    async def weather_text(self, city: str | None = None, tomorrow: bool = False) -> str:
        w = await wx_c.collect(self.env, city)
        if tomorrow and w.tomorrow_high_c is not None:
            s = (f"Tomorrow in {w.city}: {w.tomorrow_condition}, a high of {w.tomorrow_high_c:.0f} and a low of "
                 f"{w.tomorrow_low_c:.0f}")
            return s + (f", with a {w.tomorrow_rain_pct} percent chance of rain." if w.tomorrow_rain_pct >= 30 else ".")
        return wx_c.describe(w)

    async def markets_text(self, symbols: list[str] | None = None) -> str:
        return mk_c.describe(await mk_c.collect(self.env, symbols))

    async def news_text(self, topic: str = "world") -> str:
        items = await news_c.collect(self.env, topic)
        return news_c.describe(items) if items else "Nothing notable, sir."

    async def calendar_text(self, day_offset: int = 0) -> str:
        return cal_c.describe(await cal_c.collect(self.env, day_offset))
