"""Notes, Reminders, and in-process timers."""

from __future__ import annotations

import asyncio
import html
import itertools
import logging
import re
from datetime import datetime
from typing import Any

import dateparser

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import I, S, Tool, ToolContext, ToolRegistry, obj

log = logging.getLogger(__name__)


def parse_when(text: str, now: datetime) -> datetime:
    """Parse 'tomorrow 5pm', 'in 20 minutes', 'friday morning' relative to ``now`` (future-biased)."""
    t = text.strip().lower()
    t = {"morning": "9am", "afternoon": "3pm", "evening": "7pm", "tonight": "8pm"}.get(t, t)
    t = t.replace(" morning", " 9am").replace(" afternoon", " 3pm").replace(" evening", " 7pm")
    dt = dateparser.parse(
        t,
        settings={
            "RELATIVE_BASE": now.replace(tzinfo=None),
            "PREFER_DATES_FROM": "future",
            "RETURN_AS_TIMEZONE_AWARE": False,
        },
    )
    if dt is None:
        raise SkillError(f"I couldn't work out when '{text}' is, sir.")
    return dt.replace(tzinfo=now.tzinfo)


# ---- notes -----------------------------------------------------------------------


def _html_body(text: str) -> str:
    return "<br>".join(html.escape(line) for line in text.splitlines()) or ""


async def _note_create(ctx: ToolContext, a: dict[str, Any]) -> str:
    title = a["title"].strip()
    body = f"<h1>{html.escape(title)}</h1>" + _html_body(a.get("body", ""))
    await ctx.osa('on run argv\ntell application "Notes" to make new note with properties {body:(item 1 of argv)}\nend run', body)
    return f"Created a note titled {title} in the Apple Notes app (not a file on the Desktop)."


async def _note_append(ctx: ToolContext, a: dict[str, Any]) -> str:
    out = await ctx.osa(
        'on run argv\nset q to item 1 of argv\ntell application "Notes"\n'
        'set hits to (notes whose name contains q)\nif (count of hits) is 0 then return ""\n'
        "set n to item 1 of hits\nset body of n to (body of n) & (item 2 of argv)\nreturn name of n\nend tell\nend run",
        a["title"], "<br>" + _html_body(a["text"]),
    )
    if not out:
        raise SkillError(f"I couldn't find a note called {a['title']}, sir.")
    return f"Added to {out}."


_NOTE_SEARCH = """on run argv
set q to item 1 of argv
set res to ""
tell application "Notes"
set hits to (notes whose name contains q or plaintext contains q)
set n to count of hits
if n > 5 then set n to 5
repeat with i from 1 to n
set theNote to item i of hits
set txt to plaintext of theNote
if (length of txt) > 160 then set txt to text 1 thru 160 of txt
set res to res & (name of theNote) & ": " & txt & linefeed
end repeat
end tell
return res
end run"""


async def _note_search(ctx: ToolContext, a: dict[str, Any]) -> str:
    out = await ctx.osa(_NOTE_SEARCH, a["query"], timeout=40)
    return out or f"No notes mention {a['query']}."


# ---- reminders ------------------------------------------------------------------------

_MK_DATE = (
    "set d to current date\nset day of d to 1\nset year of d to (item {y} of argv as integer)\n"
    "set month of d to (item {mo} of argv as integer)\nset day of d to (item {dd} of argv as integer)\n"
    "set hours of d to (item {h} of argv as integer)\nset minutes of d to (item {mi} of argv as integer)\nset seconds of d to 0\n"
)


async def _add_reminder_due(ctx: ToolContext, title: str, due: datetime) -> None:
    script = (
        "on run argv\n" + _MK_DATE.format(y=2, mo=3, dd=4, h=5, mi=6)
        + 'tell application "Reminders" to make new reminder with properties {name:(item 1 of argv), due date:d}\nend run'
    )
    await ctx.osa(script, title, str(due.year), str(due.month), str(due.day), str(due.hour), str(due.minute))


async def _reminder_create(ctx: ToolContext, a: dict[str, Any]) -> str:
    title = a["title"].strip()
    if a.get("when"):
        due = parse_when(a["when"], ctx.clock())
        script = (
            "on run argv\n" + _MK_DATE.format(y=2, mo=3, dd=4, h=5, mi=6)
            + 'tell application "Reminders" to make new reminder with properties {name:(item 1 of argv), due date:d}\nend run'
        )
        await ctx.osa(script, title, str(due.year), str(due.month), str(due.day), str(due.hour), str(due.minute))
        import time as _time

        ends = due.timestamp()
        if 0 < ends - _time.time() <= 7 * 86400:  # Jarvis says it out loud at the time, as well as the Reminders alert
            _start_timer(ctx, title, ends, kind="reminder")
            _persist_timers(ctx)
        return f"Reminder set for {due.strftime('%A %d %B at %H:%M')}: {title}."
    await ctx.osa('on run argv\ntell application "Reminders" to make new reminder with properties {name:(item 1 of argv)}\nend run', title)
    return f"Reminder added: {title}."


async def _reminder_list(ctx: ToolContext, a: dict[str, Any]) -> str:
    out = await ctx.osa(
        'set res to ""\ntell application "Reminders"\nrepeat with r in (reminders whose completed is false)\n'
        'set res to res & (name of r) & linefeed\nend repeat\nend tell\nreturn res',
        timeout=30,
    )
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return "\n".join(lines[:15]) if lines else "No open reminders."


async def _reminder_done(ctx: ToolContext, a: dict[str, Any]) -> str:
    out = await ctx.osa(
        'on run argv\ntell application "Reminders"\nset hits to (reminders whose completed is false and name contains (item 1 of argv))\n'
        'if (count of hits) is 0 then return ""\nset completed of (item 1 of hits) to true\nreturn name of (item 1 of hits)\nend tell\nend run',
        a["title"], timeout=30,
    )
    if not out:
        raise SkillError(f"I couldn't find a reminder matching {a['title']}, sir.")
    return f"Marked {out} as done."


# ---- timers -----------------------------------------------------------------------------
# Timers run on the wall clock (a sleeping Mac stops the monotonic clock) and are saved to ~/.jarvis/timers.json, so a
# timer that ends while the lid is closed is announced on return and one survives a restart. Timers of ten minutes or
# more also become a Reminders item with an alarm, which rings even if Jarvis is not running.

_ids = itertools.count(1)
LONG_TIMER_MIN = 1.5  # minutes: from here on a timer also becomes a Reminders alarm (rings, and syncs to your iPhone)


def _timers(ctx: ToolContext) -> dict[int, dict[str, Any]]:
    return ctx.state.setdefault("timers", {})


def _timer_file():
    from jarvis_core import paths

    return paths.home() / "timers.json"


def _persist_timers(ctx: ToolContext) -> None:
    import json

    rows = [{"label": t["label"], "ends": t["ends_epoch"], "reminder": t.get("reminder", ""), "kind": t.get("kind", "timer")}
            for t in _timers(ctx).values()]
    try:
        _timer_file().write_text(json.dumps(rows))
    except OSError:
        log.warning("could not save timers", exc_info=True)


async def _finish_reminder(ctx: ToolContext, title: str) -> None:
    if not title:
        return
    try:
        await _reminder_done(ctx, {"title": title})
    except Exception:  # noqa: BLE001 - tidy-up only
        log.debug("could not complete the timer reminder", exc_info=True)


async def _timer_fire(ctx: ToolContext, tid: int, label: str, ends_epoch: float, kind: str = "timer") -> None:
    import time

    try:
        while (left := ends_epoch - time.time()) > 0:
            await asyncio.sleep(min(left, 5.0))
    except asyncio.CancelledError:
        return
    entry = _timers(ctx).pop(tid, None)
    _persist_timers(ctx)
    late = time.time() - ends_epoch
    what = f"{label} timer" if label else "timer"
    if kind == "alarm":
        msg = f"Your {label} alarm is going off, sir." if late < 30 and label else (
            "Your alarm is going off, sir." if late < 30 else "Your alarm went off while you were away, sir.")
    elif kind == "reminder":
        msg = f"A reminder, sir: {label}." if late < 30 else f"You asked me to remind you, sir: {label}. That was a little while ago."
    else:
        msg = f"Your {what} is up, sir." if late < 30 else f"Your {what} went off while you were away, sir."
    ctx.audit.write("timer_fired", label=label, late_s=round(late))
    await _finish_reminder(ctx, (entry or {}).get("reminder", ""))
    if ctx.announce:
        try:
            await ctx.announce(msg, earcon="alert")
        except Exception:
            log.exception("timer announcement failed")


def _start_timer(ctx: ToolContext, label: str, ends_epoch: float, reminder: str = "", kind: str = "timer") -> int:
    from datetime import datetime as _dt

    tid = next(_ids)
    task = asyncio.get_running_loop().create_task(_timer_fire(ctx, tid, label, ends_epoch, kind))
    _timers(ctx)[tid] = {
        "task": task, "label": label, "ends_epoch": ends_epoch, "reminder": reminder, "kind": kind,
        "ends": _dt.fromtimestamp(ends_epoch).astimezone(),
    }
    return tid


def restore_timers(ctx: ToolContext) -> int:
    """Re-arm timers saved by a previous run (called once at startup, inside the event loop)."""
    import json

    try:
        rows = json.loads(_timer_file().read_text())
    except (OSError, ValueError):
        return 0
    for r in rows:
        _start_timer(ctx, str(r.get("label", "")), float(r["ends"]), str(r.get("reminder", "")), str(r.get("kind", "timer")))
    return len(rows)


async def _timer_set(ctx: ToolContext, a: dict[str, Any]) -> str:
    import time

    minutes = float(a.get("minutes") or 0) + float(a.get("seconds") or 0) / 60
    if minutes <= 0:
        raise SkillError("For how long, sir?")
    if minutes > 24 * 60:
        raise SkillError("That's longer than a day, sir. Use a reminder instead.")
    label = (a.get("label") or "").strip()
    replaced = 0
    if a.get("replace"):  # "actually make it 40": the new length takes the place of the timer you just set
        for tid, t in list(_timers(ctx).items()):
            if t.get("kind", "timer") == "timer" and (not label or label.lower() in t["label"].lower() or not t["label"]):
                _timers(ctx).pop(tid)["task"].cancel()
                await _finish_reminder(ctx, t.get("reminder", ""))
                replaced += 1
    ends = time.time() + minutes * 60
    reminder = ""
    note = ""
    if minutes >= LONG_TIMER_MIN:
        title = f"Timer: {label or 'timer'}"
        try:
            await _add_reminder_due(ctx, title, datetime.fromtimestamp(ends))
            reminder = title
            note = " I've also set a reminder so it rings even if I'm asleep."
        except Exception:  # noqa: BLE001 - the timer itself still works
            log.info("could not add the backup reminder", exc_info=True)
    _start_timer(ctx, label, ends, reminder)
    _persist_timers(ctx)
    mins = int(minutes) if minutes == int(minutes) else round(minutes, 1)
    span = f"{round(minutes * 60)} seconds" if minutes < 1.5 else f"{mins} minutes"
    return (f"Changed the timer to {span}" if replaced else f"Timer set for {span}") + (f" ({label})." if label else ".") + (f" {note.strip()}" if note.strip() else "")


async def _alarm_set(ctx: ToolContext, a: dict[str, Any]) -> str:
    """An alarm is a wall-clock time ("6 pm"), not a length of time, so it must never go through timer_set."""
    from datetime import timedelta

    when = (a.get("time") or a.get("when") or "").strip()
    if not when:
        raise SkillError("For what time, sir?")
    now = ctx.clock()
    when = re.sub(r"\b(\d{1,2}) (\d{2})\b", r"\1:\2", when)  # the router strips colons: "7 30 am" -> "7:30 am"
    when = re.sub(r"\b(\d{1,2})\s*(a|p)\.?m\.?\b", r"\1\2m", when)
    bare = re.fullmatch(r"(?:at )?(\d{1,2})(?::(\d{2}))?", when.strip())
    if bare:  # "wake me at 6": the next 6 o'clock, am or pm
        h, m = int(bare.group(1)), int(bare.group(2) or 0)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise SkillError(f"I couldn't work out what time '{when}' is, sir.")
        cands = [now.replace(hour=hh, minute=m, second=0, microsecond=0) + timedelta(days=d)
                 for hh in ({h} if h > 12 or h == 0 else {h % 12, h % 12 + 12}) for d in (0, 1)]
        due = min(c for c in cands if c > now)
    else:
        due = parse_when(when, now)
    if due <= now:  # "6 pm" said at 7 pm means tomorrow
        due += timedelta(days=1)
    if due - now > timedelta(days=7):
        raise SkillError("That's more than a week away, sir. Use a reminder instead.")
    label = (a.get("label") or "").strip()
    title = f"Alarm: {label or due.strftime('%H:%M')}"
    reminder = ""
    try:
        await _add_reminder_due(ctx, title, due)
        reminder = title
    except Exception:  # noqa: BLE001 - Jarvis's own alarm still works
        log.info("could not add the backup reminder", exc_info=True)
    _start_timer(ctx, label, due.timestamp(), reminder, kind="alarm")
    _persist_timers(ctx)
    day = "" if due.date() == now.date() else (" tomorrow" if (due.date() - now.date()).days == 1 else due.strftime(" on %A"))
    clock = due.strftime("%-I:%M %p").replace(":00 ", " ")
    return f"Alarm set for {clock}{day}" + (f" ({label})." if label else ".")


async def _timer_list(ctx: ToolContext, a: dict[str, Any]) -> str:
    ts = {i: t for i, t in _timers(ctx).items() if t.get("kind", "timer") in ("timer", "alarm")}  # reminders are not timers
    if not ts:
        return "No timers running."
    now = ctx.clock()
    out = []
    for t in ts.values():
        if t.get("kind") == "alarm":
            out.append(f"{t['label'] or 'alarm'}: alarm at {t['ends'].strftime('%-I:%M %p')}")
            continue
        secs = max(0, int((t["ends"] - now).total_seconds()))
        left = f"{secs} seconds" if secs < 90 else f"{secs // 60} minutes"
        out.append(f"{t['label'] or 'timer'}: {left} left")
    return "\n".join(out)


async def _timer_cancel(ctx: ToolContext, a: dict[str, Any]) -> str:
    ts = _timers(ctx)
    label = (a.get("label") or "").lower()
    victims = [i for i, t in ts.items() if t.get("kind", "timer") in ("timer", "alarm") and (not label or label in t["label"].lower())]
    for i in victims:
        t = ts.pop(i)
        t["task"].cancel()
        await _finish_reminder(ctx, t.get("reminder", ""))
    _persist_timers(ctx)
    return f"Cancelled {len(victims)} timer{'s' if len(victims) != 1 else ''}." if victims else "No matching timer."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "note_create", "Create a note in Apple Notes.",
        obj({"title": S("Note title"), "body": S("Note text")}, ["title"]), _note_create, category="notes"))
    reg.register(Tool(
        "note_append", "Append text to an existing note.",
        obj({"title": S("Part of the note title"), "text": S("Text to add")}, ["title", "text"]), _note_append, category="notes"))
    reg.register(Tool(
        "note_search", "Search Apple Notes by title or content.",
        obj({"query": S("Search text")}, ["query"]), _note_search, category="notes", untrusted_output=True, timeout_s=40))
    reg.register(Tool(
        "reminder_create", "Create a reminder, optionally due at a time such as 'tomorrow 5pm'.",
        obj({"title": S("What to remember"), "when": S("Optional natural-language time")}, ["title"]),
        _reminder_create, category="reminders"))
    reg.register(Tool("reminder_list", "List open reminders.", obj(), _reminder_list, category="reminders", untrusted_output=True, timeout_s=40))
    reg.register(Tool(
        "reminder_complete", "Mark a reminder as done.",
        obj({"title": S("Part of the reminder text")}, ["title"]), _reminder_done, category="reminders", timeout_s=40))
    reg.register(Tool(
        "timer_set", "Start a countdown timer for a LENGTH of time (never a clock time; use alarm_set for that); Jarvis announces when it ends. If the user changes the length of a timer they just "
        "set ('actually make it 40', 'no, five minutes'), pass replace=true so the old one is cancelled instead of keeping two.",
        obj({"minutes": {"type": "number", "description": "Minutes"}, "seconds": I("Extra seconds"), "label": S("Optional label"),
             "replace": {"type": "boolean", "description": "Replace the running timer instead of adding another"}}),
        _timer_set, category="timers"))
    reg.register(Tool(
        "alarm_set", "Set an alarm for a clock time ('alarm at 6 pm', 'wake me at 7:30 tomorrow'). Use this, never timer_set, "
        "whenever the user gives a time of day instead of a length of time.",
        obj({"time": S("Time of day as spoken, e.g. '6 pm' or 'tomorrow 7:30am'"), "label": S("Optional label")}, ["time"]),
        _alarm_set, category="timers"))
    reg.register(Tool("timer_list", "List running timers and alarms.", obj(), _timer_list, category="timers"))
    reg.register(Tool("timer_cancel", "Cancel timers (all, or those matching a label).", obj({"label": S("Optional label")}), _timer_cancel, category="timers"))
