"""Calendar events via the Apple Calendar database (through adapters/apple_data.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from jarvis_core.briefing.env import Env
from jarvis_core.briefing.models import CalEvent


def day_bounds(now: datetime, offset_days: int = 0) -> tuple[datetime, datetime]:
    start = (now + timedelta(days=offset_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1) - timedelta(seconds=1)


async def events_between(env: Env, start: datetime, end: datetime) -> list[CalEvent]:
    provider = env.services.get("calendar_provider")
    if provider is None:
        from jarvis_core.adapters.services import calendar_events as provider  # noqa: PLC0415
    items = await asyncio.get_running_loop().run_in_executor(None, provider, start, end)
    out: list[CalEvent] = []
    for it in items:
        if not it.timestamp:
            continue
        out.append(
            CalEvent(
                id=it.id,
                title=it.title or "Untitled",
                start=it.timestamp,
                location=it.meta.get("location", ""),
                all_day=bool(it.meta.get("all_day")),
                attendees=2 if it.meta.get("has_attendees") else 0,
            )
        )
    out.sort(key=lambda e: e.start)
    return out


async def collect(env: Env, day_offset: int = 0, only_upcoming: bool = False) -> list[CalEvent]:
    now = env.now()
    start, end = day_bounds(now, day_offset)
    if only_upcoming and day_offset == 0:
        start = now
    return await events_between(env, start, end)


def spoken_time(dt: datetime) -> str:
    from jarvis_core.voice.text_norm import spoken_time as st

    return st(dt.hour, dt.minute)


def describe(events: list[CalEvent]) -> str:
    if not events:
        return "Your calendar is clear."
    parts = []
    for e in events[:6]:
        when = "all day" if e.all_day else f"at {spoken_time(e.start.astimezone())}"
        parts.append(f"{e.title} {when}")
    more = f", and {len(events) - 6} more" if len(events) > 6 else ""
    return f"You have {len(events)} event{'s' if len(events) != 1 else ''}: " + "; ".join(parts) + more + "."
