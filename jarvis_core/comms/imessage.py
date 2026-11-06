"""Read iMessage / SMS from ``~/Library/Messages/chat.db`` (read-only)."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jarvis_core.comms.sqlite_util import ReadOnlyDB
from jarvis_core.interfaces import Item

log = logging.getLogger(__name__)

DEFAULT_PATH = Path.home() / "Library/Messages/chat.db"
APPLE_EPOCH = 978307200

REQUIRED = {
    "message": {"ROWID", "text", "handle_id", "date", "is_from_me", "is_read"},
    "handle": {"ROWID", "id"},
}


def to_dt(raw: int | float | None) -> datetime | None:
    if not raw:
        return None
    secs = raw / 1e9 if raw > 1e11 else raw  # nanoseconds on modern macOS, seconds on old
    try:
        return datetime.fromtimestamp(secs + APPLE_EPOCH, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None  # a sentinel or corrupt value must not take the whole reader down


def from_dt(dt: datetime) -> int:
    return int((dt.timestamp() - APPLE_EPOCH) * 1e9)


def decode_attributed_body(blob: bytes | None) -> str:
    """Extract the plain text from an ``NSAttributedString`` typedstream blob.

    Newer macOS stores message text only here (``message.text`` is NULL).
    """
    if not blob:
        return ""
    data = bytes(blob)
    i = data.find(b"NSString")
    if i < 0:
        return ""
    rest = data[i + len(b"NSString"):]
    plus = rest.find(b"+")
    if plus < 0 or plus + 1 >= len(rest):
        return ""
    p = plus + 1
    length = rest[p]
    p += 1
    if length == 0x81:
        length = int.from_bytes(rest[p:p + 2], "little")
        p += 2
    elif length == 0x82:
        length = int.from_bytes(rest[p:p + 4], "little")
        p += 4
    elif length == 0x83:
        length = int.from_bytes(rest[p:p + 8], "little")
        p += 8
    text = rest[p:p + length].decode("utf-8", errors="replace")
    return text.replace("\ufffc", "").strip()  # drop attachment placeholders


@dataclass
class IMMessage:
    id: str
    from_me: bool
    text: str
    when: datetime | None


class IMessageDB:
    name = "imessage"

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path else DEFAULT_PATH
        self.db = ReadOnlyDB(self.path)
        self.disabled_reason = ""

    def check(self) -> tuple[bool, str]:
        if not self.path.exists():
            self.disabled_reason = "The Messages database wasn't found"
            return False, self.disabled_reason
        try:
            for table, cols in REQUIRED.items():
                have = self.db.columns(table)
                if not have:
                    self.disabled_reason = f"Messages database has no {table} table"
                    return False, self.disabled_reason
                missing = cols - have
                if missing:
                    self.disabled_reason = f"Messages schema changed ({table} lacks {', '.join(sorted(missing))})"
                    return False, self.disabled_reason
        except PermissionError:
            self.disabled_reason = "Full Disk Access is needed to read Messages"
            return False, self.disabled_reason
        self.disabled_reason = ""
        return True, ""

    def available(self) -> bool:
        return self.check()[0]

    def _rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            return self.db.connect().execute(sql, args).fetchall()
        except sqlite3.DatabaseError as e:
            log.warning("imessage query failed: %s", e)
            return []

    def _has(self, table: str, col: str) -> bool:
        return col in self.db.columns(table)

    def unread(self, since: datetime | None = None, max_items: int = 60) -> list[Item]:
        cols = self.db.columns("message")
        body_col = "m.attributedBody" if "attributedBody" in cols else "NULL"
        tapback = "AND COALESCE(m.associated_message_type,0)=0 " if "associated_message_type" in cols else ""
        room = "m.cache_roomnames" if "cache_roomnames" in cols else "NULL"
        service = "COALESCE(m.service,'')" if "service" in cols else "''"
        sql = (
            f"SELECT m.ROWID, m.text, {body_col}, m.date, h.id, {room}, {service} "
            "FROM message m LEFT JOIN handle h ON h.ROWID = m.handle_id "
            "WHERE m.is_from_me=0 AND COALESCE(m.is_read,0)=0 " + tapback
            + ("AND m.date > ? " if since else "")
            + "ORDER BY m.date DESC LIMIT ?"
        )
        args: tuple = (from_dt(since), max_items) if since else (max_items,)
        out = []
        for rowid, text, body, date, handle, room_name, service in self._rows(sql, args):
            msg = (text or "").strip() or decode_attributed_body(body)
            if not msg:
                continue
            out.append(
                Item(
                    "imessage", str(rowid), sender=handle or "Unknown", sender_id=handle or "", body=msg,
                    timestamp=to_dt(date), thread_id=room_name or handle or "", is_group=bool(room_name),
                    meta={"handle": handle or "", "service": service},
                )
            )
        return out

    def thread(self, handle: str, limit: int = 15) -> list[IMMessage]:
        cols = self.db.columns("message")
        body_col = "m.attributedBody" if "attributedBody" in cols else "NULL"
        tapback = "AND COALESCE(m.associated_message_type,0)=0 " if "associated_message_type" in cols else ""
        rows = self._rows(
            f"SELECT m.ROWID, m.is_from_me, m.text, {body_col}, m.date FROM message m "
            "JOIN handle h ON h.ROWID = m.handle_id WHERE h.id = ? " + tapback + "ORDER BY m.date DESC LIMIT ?",
            (handle, limit),
        )
        msgs = [
            IMMessage(str(rid), bool(me), (text or "").strip() or decode_attributed_body(body), to_dt(date))
            for rid, me, text, body, date in rows
        ]
        msgs = [m for m in msgs if m.text]
        msgs.reverse()
        return msgs

    def handles(self) -> list[str]:
        return [r[0] for r in self._rows("SELECT DISTINCT id FROM handle WHERE id IS NOT NULL")]

    def all_sent(self, n: int = 200) -> list[str]:
        """Your own recent text messages across every conversation (for the weekly style refresh)."""
        rows = self._rows(
            "SELECT text FROM message WHERE is_from_me=1 AND text IS NOT NULL AND text != '' ORDER BY date DESC LIMIT ?", (n,))
        return [r[0] for r in rows]

    def sent_samples(self, handle: str, n: int = 6) -> list[str]:
        return [m.text for m in reversed(self.thread(handle, 60)) if m.from_me][:n]
