"""Read the WhatsApp desktop app's local ``ChatStorage.sqlite`` (read-only)."""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jarvis_core.comms.sqlite_util import ReadOnlyDB
from jarvis_core.interfaces import Item

log = logging.getLogger(__name__)

CORE_DATA_EPOCH = 978307200  # 2001-01-01 in unix seconds

DEFAULT_PATHS = [
    Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
    Path.home() / "Library/Containers/net.whatsapp.WhatsApp/Data/Library/Application Support/WhatsApp/ChatStorage.sqlite",
]

REQUIRED = {
    "ZWAMESSAGE": {"Z_PK", "ZCHATSESSION", "ZISFROMME", "ZTEXT", "ZMESSAGEDATE", "ZMESSAGETYPE"},
    "ZWACHATSESSION": {"Z_PK", "ZCONTACTJID", "ZPARTNERNAME", "ZUNREADCOUNT"},
}

MEDIA_LABEL = {1: "a photo", 2: "a video", 3: "a voice note", 4: "a contact", 5: "a location", 7: "a link",
               8: "a document", 11: "a GIF", 14: "a deleted message", 15: "a sticker"}


def to_dt(core_data_seconds: float | None) -> datetime | None:
    """Core Data seconds -> UTC datetime; ``None`` for missing or nonsense values (WhatsApp stores sentinels)."""
    if core_data_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(float(core_data_seconds) + CORE_DATA_EPOCH, tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def from_dt(dt: datetime) -> float:
    return dt.timestamp() - CORE_DATA_EPOCH


def jid_to_phone(jid: str) -> str:
    """'919876543210@s.whatsapp.net' -> '+919876543210' (groups and unknown JIDs give '')."""
    if not jid or not jid.endswith("@s.whatsapp.net"):
        return ""
    digits = re.sub(r"\D", "", jid.split("@")[0])
    return f"+{digits}" if digits else ""


def phone_to_jid(e164: str) -> str:
    return re.sub(r"\D", "", e164) + "@s.whatsapp.net"


@dataclass
class WAChat:
    jid: str
    name: str
    is_group: bool
    unread: int
    last_message: datetime | None = None

    @property
    def phone(self) -> str:
        return jid_to_phone(self.jid)


@dataclass
class WAMessage:
    id: str
    from_me: bool
    text: str
    when: datetime | None
    sender: str = ""


class WhatsAppDB:
    name = "whatsapp"

    def __init__(self, path: Path | str | None = None) -> None:
        paths = [Path(path).expanduser()] if path else DEFAULT_PATHS
        self.path = next((p for p in paths if p.exists()), paths[0])
        self.db = ReadOnlyDB(self.path)
        self.disabled_reason = ""

    # ---- health ---------------------------------------------------------------------------
    def check(self) -> tuple[bool, str]:
        """(ok, reason). Turns the reader off cleanly when the schema isn't what we expect."""
        if not self.path.exists():
            self.disabled_reason = "WhatsApp's database wasn't found (is the desktop app installed and signed in?)"
            return False, self.disabled_reason
        try:
            for table, cols in REQUIRED.items():
                have = self.db.columns(table)
                if not have:
                    self.disabled_reason = f"WhatsApp database has no {table} table"
                    return False, self.disabled_reason
                missing = cols - have
                if missing:
                    self.disabled_reason = f"WhatsApp schema changed ({table} lacks {', '.join(sorted(missing))})"
                    return False, self.disabled_reason
        except PermissionError:
            self.disabled_reason = "Full Disk Access is needed to read WhatsApp"
            return False, self.disabled_reason
        self.disabled_reason = ""
        return True, ""

    def available(self) -> bool:
        return self.check()[0]

    # ---- reading ------------------------------------------------------------------------------
    def _rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            return self.db.connect().execute(sql, args).fetchall()
        except sqlite3.DatabaseError as e:
            log.warning("whatsapp query failed: %s", e)
            return []

    def chats(self, limit: int = 200) -> list[WAChat]:
        rows = self._rows(
            "SELECT ZCONTACTJID, ZPARTNERNAME, ZUNREADCOUNT, ZLASTMESSAGEDATE FROM ZWACHATSESSION "
            "WHERE ZCONTACTJID IS NOT NULL AND ZCONTACTJID NOT LIKE 'status@%' AND ZCONTACTJID NOT LIKE '%@broadcast' "
            "AND COALESCE(ZARCHIVED,0)=0 ORDER BY ZLASTMESSAGEDATE DESC LIMIT ?",
            (limit,),
        )
        return [
            WAChat(jid, name or jid_to_phone(jid) or jid.split("@")[0], jid.endswith("@g.us"), int(unread or 0), to_dt(last))
            for jid, name, unread, last in rows
        ]

    def chat_by_jid(self, jid: str) -> WAChat | None:
        return next((c for c in self.chats() if c.jid == jid), None)

    def _sender_names(self) -> dict[str, str]:
        rows = self._rows("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME") if self.db.columns("ZWAPROFILEPUSHNAME") else []
        return {jid: name for jid, name in rows if jid and name}

    def unread(self, since: datetime | None = None, max_items: int = 60) -> list[Item]:
        """Incoming messages in chats that still have unread counts (newest ``unread`` per chat)."""
        out: list[Item] = []
        names = self._sender_names()
        has_group_member = bool(self.db.columns("ZWAGROUPMEMBER"))
        for chat in self.chats():
            if chat.unread <= 0:
                continue
            sql = (
                "SELECT m.Z_PK, m.ZTEXT, m.ZMESSAGEDATE, m.ZMESSAGETYPE, m.ZFROMJID, "
                + ("g.ZMEMBERJID " if has_group_member else "NULL ")
                + "FROM ZWAMESSAGE m "
                + ("LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER " if has_group_member else "")
                + "WHERE m.ZCHATSESSION = (SELECT Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID=? LIMIT 1) "
                "AND COALESCE(m.ZISFROMME,0)=0 ORDER BY m.ZMESSAGEDATE DESC LIMIT ?"
            )
            for pk, text, date, mtype, from_jid, member_jid in self._rows(sql, (chat.jid, chat.unread)):
                when = to_dt(date)
                if since and when and when < since:
                    continue
                body = (text or "").strip() or ("sent " + MEDIA_LABEL.get(mtype or 0, "an attachment") if mtype else "")
                if chat.is_group:
                    mjid = member_jid or from_jid or ""
                    who = names.get(mjid) or jid_to_phone(mjid) or "Someone"
                    sender = f"{who} in {chat.name}"
                else:
                    sender = chat.name
                out.append(
                    Item(
                        "whatsapp", str(pk), sender=sender, sender_id=chat.jid, body=body, timestamp=when,
                        thread_id=chat.jid, is_group=chat.is_group,
                        meta={"chat": chat.name, "phone": chat.phone, "jid": chat.jid},
                    )
                )
        out.sort(key=lambda i: i.timestamp or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out[:max_items]

    def thread(self, jid: str, limit: int = 15) -> list[WAMessage]:
        rows = self._rows(
            "SELECT m.Z_PK, m.ZISFROMME, m.ZTEXT, m.ZMESSAGEDATE, m.ZMESSAGETYPE FROM ZWAMESSAGE m "
            "WHERE m.ZCHATSESSION = (SELECT Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID=? LIMIT 1) "
            "ORDER BY m.ZMESSAGEDATE DESC LIMIT ?",
            (jid, limit),
        )
        msgs = [
            WAMessage(str(pk), bool(me), (text or "").strip() or ("[" + MEDIA_LABEL.get(mt or 0, "attachment") + "]"), to_dt(date))
            for pk, me, text, date, mt in rows
        ]
        msgs.reverse()
        return msgs

    def all_sent(self, n: int = 200) -> list[str]:
        """Your own recent text messages across every chat (for the weekly style refresh)."""
        rows = self._rows(
            "SELECT ZTEXT FROM ZWAMESSAGE WHERE ZISFROMME=1 AND ZTEXT IS NOT NULL AND ZTEXT != '' "
            "ORDER BY ZMESSAGEDATE DESC LIMIT ?", (n,))
        return [r[0] for r in rows]

    def sent_samples(self, jid: str, n: int = 6) -> list[str]:
        """Your own recent text messages in this chat, for matching your style."""
        rows = self._rows(
            "SELECT m.ZTEXT FROM ZWAMESSAGE m WHERE m.ZCHATSESSION = "
            "(SELECT Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID=? LIMIT 1) "
            "AND m.ZISFROMME=1 AND m.ZTEXT IS NOT NULL AND m.ZTEXT != '' ORDER BY m.ZMESSAGEDATE DESC LIMIT ?",
            (jid, n),
        )
        return [r[0] for r in rows]
