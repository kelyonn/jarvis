"""Read the macOS Calendar and Contacts databases directly (read-only SQLite; needs Full Disk Access).

The schema handling follows the open-source OpenJarvis connectors (Apache-2.0, https://github.com/open-jarvis/OpenJarvis).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis_core.interfaces import Item

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
CAL_DB = Path.home() / "Library/Group Containers/group.com.apple.calendar/Calendar.sqlitedb"
ADDRESSBOOK_DIR = Path.home() / "Library/Application Support/AddressBook"


def _ts(seconds: float) -> datetime:
    return _APPLE_EPOCH + timedelta(seconds=seconds)


def _to_apple(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - _APPLE_EPOCH).total_seconds()


def _open(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _wall_time(start: float, tz: str | None, all_day: bool) -> datetime:
    """Calendar stores wall-clock semantics: all-day and floating events carry no timezone."""
    dt = _ts(start)
    if all_day or not tz or tz == "_float":
        return dt.replace(tzinfo=None).astimezone()
    try:
        return dt.astimezone(ZoneInfo(tz))
    except ZoneInfoNotFoundError:
        return dt


_COLS = """ci.ROWID, ci.summary, {s}, {e}, ci.all_day, ci.start_tz, ci.location_id, ci.has_attendees, ci.UUID, c.title"""
_PLAIN = f"""SELECT {_COLS.format(s='ci.start_date', e='ci.end_date')} FROM CalendarItem ci JOIN Calendar c ON ci.calendar_id = c.ROWID
WHERE ci.start_date BETWEEN ? AND ? AND ci.summary IS NOT NULL AND ci.hidden = 0
  AND NOT EXISTS (SELECT 1 FROM OccurrenceCache m WHERE m.event_id = ci.ROWID AND m.occurrence_start_date = ci.start_date)"""
_OCC = f"""SELECT {_COLS.format(s='oc.occurrence_start_date', e='oc.occurrence_end_date')} FROM OccurrenceCache oc
JOIN CalendarItem ci ON oc.event_id = ci.ROWID JOIN Calendar c ON oc.calendar_id = c.ROWID
WHERE oc.occurrence_end_date >= ? AND oc.occurrence_start_date <= ? AND ci.summary IS NOT NULL AND ci.hidden = 0"""


def calendar_events(start: datetime, end: datetime, db_path: Path | None = None) -> list[Item]:
    """Events (including each occurrence of recurring ones) that start inside ``[start, end]``."""
    path = db_path or CAL_DB
    conn = _open(path)
    try:
        lo, hi = _to_apple(start), _to_apple(end)
        has_occ = {"event_id", "calendar_id", "occurrence_start_date", "occurrence_end_date"} <= {
            r[1] for r in conn.execute("PRAGMA table_info(OccurrenceCache)")}
        rows = conn.execute(_PLAIN, (lo, hi)).fetchall()
        if has_occ:
            rows += conn.execute(_OCC, (lo, hi)).fetchall()
        items: list[Item] = []
        seen: set[str] = set()
        for rowid, summary, s_ts, e_ts, all_day, tz, loc_id, attendees, uuid, cal in rows:
            when = _wall_time(s_ts, tz, bool(all_day))
            if not (start <= when <= end):
                continue
            ident = f"apple_calendar:{uuid or rowid}:{int(s_ts)}"
            if ident in seen:
                continue
            seen.add(ident)
            location = ""
            if loc_id:
                row = conn.execute("SELECT title, address FROM Location WHERE ROWID = ?", (loc_id,)).fetchone()
                location = ", ".join(p for p in (row or ()) if p)
            items.append(Item(
                source="calendar", id=ident, title=summary or "", body="", timestamp=when, unread=False,
                meta={"calendar": cal or "", "all_day": bool(all_day), "location": location, "has_attendees": bool(attendees),
                      "end": _wall_time(e_ts, tz, bool(all_day)) if e_ts is not None else None}))
    finally:
        conn.close()
    items.sort(key=lambda i: i.timestamp or datetime.max.replace(tzinfo=timezone.utc))
    return items


def _label(raw: str | None) -> str:
    return raw[4:-4] if raw and raw.startswith("_$!<") and raw.endswith(">!$_") else (raw or "")


def apple_contacts() -> list[dict[str, Any]]:
    """Name, phones and emails for every contact, across the main and synced (iCloud...) address books."""
    dbs = [p for p in [ADDRESSBOOK_DIR / "AddressBook-v22.abcddb",
                       *sorted((ADDRESSBOOK_DIR / "Sources").glob("*/AddressBook-v22.abcddb"))] if p.exists()]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in dbs:
        try:
            conn = _open(path)
        except sqlite3.OperationalError:
            continue
        try:
            rows = conn.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZORGANIZATION, ZNICKNAME, ZUNIQUEID FROM ZABCDRECORD "
                "WHERE ZFIRSTNAME IS NOT NULL OR ZLASTNAME IS NOT NULL OR ZORGANIZATION IS NOT NULL").fetchall()
            for pk, first, middle, last, org, nick, uid in rows:
                key = uid or f"{path}:{pk}"
                if key in seen:
                    continue
                seen.add(key)
                name = " ".join(p for p in (first, middle, last) if p) or (org or "")
                phones = [n for (n, _l) in conn.execute("SELECT ZFULLNUMBER, ZLABEL FROM ZABCDPHONENUMBER WHERE ZOWNER = ?", (pk,)) if n]
                emails = [a for (a, _l) in conn.execute("SELECT ZADDRESS, ZLABEL FROM ZABCDEMAILADDRESS WHERE ZOWNER = ?", (pk,)) if a]
                lines = [name, *(f"Phone: {p}" for p in phones), *(f"Email: {e}" for e in emails)]
                out.append({"name": name, "content": "\n".join(lines), "participants": [],
                            "metadata": {"name": name, "nickname": nick or "", "organization": org or ""}})
        finally:
            conn.close()
    return out
