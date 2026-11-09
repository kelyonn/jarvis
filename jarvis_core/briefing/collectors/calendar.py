"""Calendar events via the Apple Calendar database (through adapters/apple_data.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from jarvis_core.briefing.env import Env
from jarvis_core.briefing.models import CalEvent


def day_bounds(now: datetime, offset_days: int = 0) -> tuple[datetime, datetime]:
    start = (now + timedelta(days=offset_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1) - timedelta(seconds=1)


def _google_events(start: datetime, end: datetime) -> list:
    """Items for the same window from Google Calendar (the sign-in Gmail already uses); [] when not connected."""
    from jarvis_core.adapters.workspace import events_all  # noqa: PLC0415
    from jarvis_core.interfaces import Item  # noqa: PLC0415

    return [
        Item(source="calendar", id=f"gcal:{e.id}", title=e.title, body="", timestamp=e.start, unread=False,
             meta={"location": e.location, "all_day": e.all_day, "has_attendees": e.attendees > 1, "end": e.end})
        for e in events_all(start, end, limit=50) if e.start
    ]


async def events_between(env: Env, start: datetime, end: datetime) -> list[CalEvent]:
    """Events from the Apple Calendar database and from Google Calendar, merged. Either source may be unavailable (no Full
    Disk Access, not signed in to Google); the section only fails when every source does."""
    provider = env.services.get("calendar_provider")
    loop = asyncio.get_running_loop()
    if provider is not None:
        sources = [provider]
    else:
        from jarvis_core.adapters.services import calendar_events as apple  # noqa: PLC0415

        sources = [apple, _google_events]
    items: list = []
    errors: list[Exception] = []
    for src in sources:
        try:
            items += await loop.run_in_executor(None, src, start, end)
        except Exception as e:  # noqa: BLE001 - one source down must not hide the other
            errors.append(e)
    if errors and len(errors) == len(sources):
        raise errors[0]
    out: list[CalEvent] = []
    seen: set[tuple[str, int]] = set()
    for it in items:
        if not it.timestamp:
            continue
        key = ((it.title or "").strip().lower(), int(it.timestamp.timestamp() // 60))
        if key in seen:
            continue
        seen.add(key)
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
