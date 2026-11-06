"""The six alert rules. Each rule inspects the world and returns alerts; the engine handles
de-duplication, throttling, presence and delivery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
from datetime import timedelta
from typing import Protocol

import psutil

from jarvis_core.alerts.models import Alert
from jarvis_core.briefing.collectors import calendar, markets, news, weather
from jarvis_core.briefing.env import Env

log = logging.getLogger(__name__)

SOURCE_LABEL = {"whatsapp": "WhatsApp", "imessage": "iMessage", "gmail": "email", "linkedin": "LinkedIn"}


class Rule(Protocol):
    kind: str

    async def check(self, env: Env) -> list[Alert]: ...


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# ---- 1. meeting ---------------------------------------------------------------------------


def meeting_speech(title: str, start_ts: float, now_ts: float, location: str = "") -> str:
    mins = max(0, math.ceil((start_ts - now_ts) / 60))
    when = "is starting now" if mins <= 0 else f"starts in {_plural(mins, 'minute')}"
    loc = f", at {location}" if location else ""
    return f"Sir, {title} {when}{loc}."


class MeetingRule:
    kind = "meeting"

    async def check(self, env: Env) -> list[Alert]:
        now = env.now()
        lead = env.cfg.alerts.meeting_lead_minutes
        events = await calendar.events_between(env, now, now + timedelta(minutes=lead))
        out = []
        for ev in events:
            if ev.all_day or ev.start <= now - timedelta(minutes=1):
                continue
            ts = ev.start.timestamp()
            out.append(
                Alert(
                    key=f"meeting:{ev.id}:{int(ts)}",
                    kind=self.kind,
                    title=f"{ev.title} at {calendar.spoken_time(ev.start)}",
                    speech=meeting_speech(ev.title, ts, now.timestamp(), ev.location),
                    priority="high",
                    expires=ts + 120,
                    payload={"start": ts, "title": ev.title, "location": ev.location},
                )
            )
        return out


# ---- 2. VIP message / email ----------------------------------------------------------------


class VipRule:
    kind = "vip"

    def __init__(self, lookback_minutes: int = 45) -> None:
        self.lookback = timedelta(minutes=lookback_minutes)

    async def check(self, env: Env) -> list[Alert]:
        hub = env.services.get("comms")
        if hub is None:
            return []
        # LinkedIn is never polled: reading it means driving the browser, which only happens on request
        sources = [s for s in env.cfg.messaging.apps if s in SOURCE_LABEL and s != "linkedin"]
        offers = env.services.get("offers")
        items = await hub.unread(since=env.now() - self.lookback, sources=sources)
        out = []
        for it in items:
            _score, vip = hub.importance(it)
            if not vip:
                continue
            label = SOURCE_LABEL.get(it.source, it.source)
            sender = it.sender or "Someone"
            if it.source == "gmail":
                speech = f"Sir, {sender} has emailed you: {it.title or (it.body or '')[:100]}."
            else:
                body = " ".join((it.body or "").split())[:140]
                speech = f"Sir, {sender} has messaged you on {label}" + (f": {body}" if body else ".")
            offer = env.cfg.messaging.proactive_reply_offers and (offers is None or offers.allowed(it.thread_id or it.id))
            if offer:
                speech += " Shall I draft a reply?"
            out.append(
                Alert(
                    key=f"vip:{it.source}:{it.id}",
                    kind=self.kind,
                    title=f"{sender} · {label}",
                    speech=speech,
                    priority="urgent",
                    expires=env.now().timestamp() + 6 * 3600,
                    follow_up=bool(offer),
                    payload={"offer": {"type": "reply", "source": it.source, "item_id": it.id, "sender": sender,
                                       "thread_id": it.thread_id or it.id}} if offer else {},
                )
            )
        return out


# ---- 3. market move -------------------------------------------------------------------------


class MarketRule:
    kind = "market"

    async def check(self, env: Env) -> list[Alert]:
        cfg = env.cfg
        idx_t, cry_t = cfg.alerts.market_index_move_pct, cfg.alerts.crypto_move_pct
        quotes = await markets.collect(env, [*cfg.markets.indices, *cfg.markets.crypto])
        crypto = set(cfg.markets.crypto)
        day = env.now().date().isoformat()
        out = []
        for q in quotes:
            threshold = cry_t if q.symbol in crypto else idx_t
            pct = q.change_pct
            if abs(pct) < threshold:
                continue
            bucket = int(abs(pct) // threshold)  # alerts again only when the move doubles, triples...
            direction = "up" if pct > 0 else "down"
            verb = "jumped" if pct > 0 else "dropped"
            out.append(
                Alert(
                    key=f"market:{q.symbol}:{day}:{direction}:{bucket}",
                    kind=self.kind,
                    title=f"{q.name or q.symbol} {pct:+.1f}%",
                    speech=f"Sir, {q.name or q.symbol} has {verb} {abs(pct):.1f} percent, now at {q.price:,.0f}.",
                    priority="normal",
                    expires=env.now().timestamp() + 4 * 3600,
                    payload={"symbol": q.symbol, "pct": round(pct, 2)},
                )
            )
        out.sort(key=lambda a: -abs(a.payload["pct"]))
        return out


# ---- 4. rain ----------------------------------------------------------------------------------


class RainRule:
    kind = "rain"

    async def check(self, env: Env) -> list[Alert]:
        a = env.cfg.alerts
        w = await weather.collect(env)
        off = w.raining_soon(a.rain_lookahead_hours, a.rain_probability_pct)
        if off is None:
            return []
        now = env.now()
        slot = now + timedelta(hours=off)
        prob = max((p for o, p in w.rain_next_hours if o == off), default=a.rain_probability_pct)
        if off <= 0:
            when = "is likely right now"
        elif off == 1:
            when = "is likely within the hour"
        else:
            when = f"is likely in about {off} hours"
        return [
            Alert(
                key=f"rain:{slot.date().isoformat()}:{slot.hour}",
                kind=self.kind,
                title=f"Rain, {prob}% chance",
                speech=f"Sir, rain {when} in {w.city}, around {prob} percent. You may want an umbrella.",
                priority="normal",
                expires=now.timestamp() + 2 * 3600,
                payload={"prob": prob, "offset": off},
            )
        ]


# ---- 5. breaking news ---------------------------------------------------------------------------


def _tkey(title: str) -> str:
    return hashlib.sha1(" ".join(sorted(news.tokens(title))).encode()).hexdigest()[:16]


class BreakingNewsRule:
    kind = "news"

    def __init__(self, fresh_minutes: int = 120) -> None:
        self.fresh = timedelta(minutes=fresh_minutes)

    async def check(self, env: Env) -> list[Alert]:
        cfg = env.cfg
        urls = [*cfg.news.world, *cfg.news.india]
        results = await asyncio.gather(*(news.fetch_feed(env, u) for u in urls), return_exceptions=True)
        items = [h for r in results if isinstance(r, list) for h in r]
        if not items:
            return []
        now = env.now()
        need = cfg.alerts.breaking_news_min_sources
        out = []
        for group in news.cluster(items):
            sources = {h.source for h in group}
            if len(sources) < need:
                continue
            dated = [h for h in group if h.published]
            if not dated or max(h.published for h in dated) < now - self.fresh:
                continue
            hashes = [_tkey(h.title) for h in group]
            if any(env.store.seen("news_alert", h) for h in hashes):
                continue
            oldest = min(dated, key=lambda h: h.published)
            best = max(dated, key=lambda h: len(h.title))
            out.append(
                Alert(
                    key=f"news:{_tkey(oldest.title)}",
                    kind=self.kind,
                    title=news.clean_title(best.title)[:80],
                    speech=f"Breaking, sir. {news.clean_title(best.title).rstrip('.')}. Reported by {len(sources)} outlets.",
                    priority="normal",
                    expires=now.timestamp() + 3 * 3600,
                    payload={"seen_ids": hashes, "seen_kind": "news_alert"},
                )
            )
            break  # one at a time
        return out


# ---- 6. battery ---------------------------------------------------------------------------------


class BatteryRule:
    kind = "battery"

    def __init__(self, sensor=psutil.sensors_battery) -> None:
        self._sensor = sensor

    async def check(self, env: Env) -> list[Alert]:
        b = self._sensor()
        if b is None or b.power_plugged:
            return []
        pct = round(b.percent)
        low = env.cfg.alerts.battery_low_pct
        if pct > low:
            return []
        day = env.now().date().isoformat()
        critical = pct <= 5
        return [
            Alert(
                key=f"battery:{day}:{pct // 5}",
                kind=self.kind,
                title=f"Battery {pct}%",
                speech=f"Sir, battery is at {pct} percent. " + ("Please plug in right away." if critical else "Perhaps it's time to plug in."),
                priority="urgent" if critical else "high",
                expires=env.now().timestamp() + 20 * 60,
                payload={"percent": pct},
            )
        ]


def default_rules() -> list[Rule]:
    return [MeetingRule(), VipRule(), MarketRule(), RainRule(), BreakingNewsRule(), BatteryRule()]
