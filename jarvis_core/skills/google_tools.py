"""Google Calendar and Drive tools (same Google sign-in as Gmail)."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import Any

from jarvis_core import cards
from jarvis_core.adapters.workspace import CAL_SCOPE, Workspace, WorkspaceError, events_all
from jarvis_core.errors import SkillError
from jarvis_core.skills.base import Risk, S, Tool, ToolContext, ToolRegistry, obj


def _ws(ctx: ToolContext) -> Workspace:
    return ctx.services.get("workspace") or Workspace()


async def _run(fn, *a: Any, **kw: Any):
    try:
        return await asyncio.to_thread(fn, *a, **kw)
    except WorkspaceError as e:
        raise SkillError(str(e)) from e


def _day_bounds(day: str) -> tuple[datetime, datetime, str]:
    now = datetime.now().astimezone()
    day = (day or "today").lower()
    if day == "tomorrow":
        d = now.date() + timedelta(days=1)
        label = "tomorrow"
    elif day in ("week", "this week", "next 7 days"):
        return now, now + timedelta(days=7), "over the next week"
    else:
        d = now.date()
        label = "today"
    start = datetime.combine(d, time.min, tzinfo=now.tzinfo)
    return (max(start, now) if label == "today" else start), start + timedelta(days=1), label


async def _agenda(ctx: ToolContext, a: dict[str, Any]) -> str:
    ws = _ws(ctx)
    start, end, label = _day_bounds(a.get("day") or "today")
    if not ws.session.connected() or not ws.has(CAL_SCOPE):  # not on Google yet: fall back to the Mac's Calendar app
        briefing = ctx.services.get("briefing")
        if briefing is not None:
            text = await briefing.calendar_text(1 if label == "tomorrow" else 0)
            card, briefing.last_card = briefing.last_card, None
            if card is not None:
                cards.post(ctx, card)
            return text
    events = await _run(events_all if ctx.services.get("workspace") is None else ws.events, start, end)
    if not events:
        return f"Nothing on your calendar {label}."
    cards.post(ctx, cards.calendar_card(events, label.capitalize()))
    return f"Your calendar {label}:\n" + "\n".join(e.line() for e in events)


async def _search(ctx: ToolContext, a: dict[str, Any]) -> str:
    now = datetime.now().astimezone()
    fn = events_all if ctx.services.get("workspace") is None else _ws(ctx).events
    events = await _run(fn, now - timedelta(days=30), now + timedelta(days=180), a["query"], 10)
    return "Matching events:\n" + "\n".join(e.line() for e in events) if events else f"Nothing on your calendar matches {a['query']}."


def _parse_when(text: str) -> datetime:
    try:
        d = datetime.fromisoformat(text.strip())
    except ValueError as e:
        raise SkillError("I need the start time as a date and time, like 2026-11-06 15:00.") from e
    return d.astimezone() if d.tzinfo is None else d


async def _add_event(ctx: ToolContext, a: dict[str, Any]) -> str:
    start = _parse_when(a["start"])
    ev = await _run(_ws(ctx).create_event, a["title"], start, int(a.get("minutes") or 60), a.get("location", ""), a.get("notes", ""))
    return f"Added {ev.title} for {start.strftime('%A %d %B at %I:%M %p').replace(' 0', ' ')}."


async def _drive_search(ctx: ToolContext, a: dict[str, Any]) -> str:
    ws = _ws(ctx)
    q = (a.get("query") or "").strip()
    files = await _run(ws.search_files if q else ws.recent_files, *([q] if q else []))
    if not files:
        return f"Nothing in Drive matches {q}." if q else "Your Drive looks empty."
    return "Drive files (id, then details):\n" + "\n".join(f"{f.id} | {f.line()}" for f in files)


async def _drive_read(ctx: ToolContext, a: dict[str, Any]) -> str:
    return await _run(_ws(ctx).read_file, a["file_id"])


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "gcal_agenda", "The user's Google Calendar events for today, tomorrow or the next week (falls back to the Mac Calendar).",
        obj({"day": S("today, tomorrow or week")}), _agenda, category="calendar", untrusted_output=True))
    reg.register(Tool(
        "gcal_search", "Search the user's Google Calendar (past month to next six) for events matching a word or name.",
        obj({"query": S("Word or name to look for")}, ["query"]), _search, category="calendar", untrusted_output=True))
    reg.register(Tool(
        "gcal_add_event", "Add an event to the user's Google Calendar. Start is local date and time like 2026-11-06T15:00.",
        obj({"title": S("Event title"), "start": S("Start, ISO local time"), "minutes": S("Length in minutes, default 60"),
             "location": S("Optional place"), "notes": S("Optional notes")}, ["title", "start"]),
        _add_event, risk=Risk.CONFIRM, category="calendar",
        confirm_prompt=lambda a: f"Add {a.get('title', 'this event')} on {a.get('start', '')} to your calendar?"))
    reg.register(Tool(
        "gdrive_search", "Search the user's Google Drive by name or content; with no query lists recently edited files. Returns file ids.",
        obj({"query": S("What to look for; omit for recent files")}), _drive_search, category="drive", untrusted_output=True))
    reg.register(Tool(
        "gdrive_read", "Read the text of a Google Doc, Sheet, Slides or text file from Drive, using an id from gdrive_search.",
        obj({"file_id": S("Drive file id")}, ["file_id"]), _drive_read, category="drive", untrusted_output=True, timeout_s=45))
