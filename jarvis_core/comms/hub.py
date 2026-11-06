"""The comms hub: one place to ask "what's new?", "who is this?", "what was said?" across all channels."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jarvis_core.comms.contacts import Contact, ContactBook, normalise_phone
from jarvis_core.comms.gmail import is_promotional
from jarvis_core.comms.vip import VipList
from jarvis_core.config import Config
from jarvis_core.interfaces import Item

log = logging.getLogger(__name__)

# how long a source's unread list is reused before asking again
TTL_S = {"whatsapp": 20.0, "imessage": 20.0, "gmail": 90.0, "linkedin": 1800.0}
URGENT_RE = re.compile(r"\b(urgent|asap|emergency|important|immediately|right now|call me|please call|need you|help)\b", re.I)
TIME_RE = re.compile(r"\b(today|tonight|tomorrow|this evening|this morning|deadline|meeting|meet|dinner|lunch)\b", re.I)
EMAIL_ASK_RE = re.compile(r"\b(can you|could you|would you|please|let me know|reply|respond|confirm|are you free)\b", re.I)


@dataclass
class ThreadMsg:
    from_me: bool
    text: str
    when: datetime | None = None


@dataclass
class Channel:
    """Where a draft goes."""

    app: str  # whatsapp | imessage | gmail | linkedin
    ident: str  # jid | handle | email | name
    name: str
    thread_id: str = ""
    phone: str = ""
    email: str = ""
    subject: str = ""
    in_reply_to: str = ""
    references: str = ""
    is_group: bool = False


class CommsHub:
    def __init__(
        self,
        cfg: Config,
        *,
        contacts: ContactBook,
        vip: VipList,
        whatsapp: Any = None,
        imessage: Any = None,
        gmail: Any = None,
        linkedin: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.contacts = contacts
        self.vip = vip
        self.readers: dict[str, Any] = {k: v for k, v in
                                        {"whatsapp": whatsapp, "imessage": imessage, "gmail": gmail, "linkedin": linkedin}.items() if v}
        self._clock = clock
        self._cache: dict[str, tuple[float, list[Item]]] = {}
        self._errors: dict[str, str] = {}
        self.missing: dict[str, str] = {}  # sources that couldn't be set up, with the reason

    # ---- status ------------------------------------------------------------------------------
    def status(self) -> dict[str, tuple[bool, str]]:
        out = {}
        for name in ("whatsapp", "imessage", "gmail", "linkedin"):
            reader = self.readers.get(name)
            if reader is None:
                out[name] = (False, self.missing.get(name, "not configured"))
                continue
            ok, reason = reader.check()
            out[name] = (ok and name not in self._errors, reason or self._errors.get(name, ""))
        return out

    def enabled_sources(self) -> list[str]:
        return [s for s in self.cfg.messaging.apps if s in self.readers]

    # ---- reading --------------------------------------------------------------------------------
    async def _fetch(self, source: str) -> list[Item]:
        reader = self.readers[source]
        ttl = TTL_S.get(source, 30.0)
        hit = self._cache.get(source)
        if hit and self._clock() - hit[0] < ttl:
            return hit[1]
        ok, reason = reader.check()
        if not ok:
            self._errors[source] = reason
            return []
        try:
            if asyncio.iscoroutinefunction(reader.unread):
                items = await reader.unread()
            else:
                items = await asyncio.to_thread(reader.unread)
        except PermissionError:
            self._errors[source] = "Full Disk Access is needed"
            return []
        except Exception as e:
            log.warning("%s unread failed: %r", source, e)
            self._errors[source] = str(e)
            return hit[1] if hit else []
        self._errors.pop(source, None)
        for it in items:
            self._label(it)
        self._cache[source] = (self._clock(), items)
        return items

    def _label(self, item: Item) -> None:
        """Replace bare phone numbers / handles with contact names."""
        if item.source == "imessage":
            name = self.contacts.name_for(item.sender_id)
            if name:
                item.sender = name
        elif item.source == "whatsapp" and not item.is_group and item.sender.startswith("+"):
            name = self.contacts.name_for(item.sender_id) or self.contacts.name_for(item.meta.get("phone", ""))
            if name:
                item.sender = name

    async def unread(self, since: datetime | None = None, sources: list[str] | None = None) -> list[Item]:
        wanted = [s for s in (sources or self.enabled_sources()) if s in self.readers]
        results = await asyncio.gather(*(self._fetch(s) for s in wanted))
        items = [i for group in results for i in group]
        if since is not None:
            cutoff = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            items = [i for i in items if i.timestamp is None or i.timestamp >= cutoff]
        items.sort(key=lambda i: i.timestamp or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return items

    # ---- importance --------------------------------------------------------------------------------
    def _ids(self, item: Item) -> list[str]:
        ids = [item.sender_id, item.meta.get("phone", ""), item.meta.get("handle", "")]
        c = self.contacts.by_identifier(item.sender_id) or self.contacts.by_identifier(item.meta.get("phone", ""))
        if c:
            ids += c.identifiers()
        return [i for i in ids if i]

    def importance(self, item: Item) -> tuple[int, bool]:
        """(score, is_vip). Score >= 2 counts as important; VIPs score 5+."""
        text = f"{item.title} {item.body}"
        score = 0
        vip = False
        known = bool(self.contacts.by_identifier(item.sender_id))
        if self.vip.is_vip(item.sender, self._ids(item)):
            if item.is_group:
                score += 1  # a VIP talking in a group isn't a personal ping
            else:
                vip, score = True, score + 5
        if not item.is_group:
            score += 1
        else:
            score -= 1
        if "?" in text:
            score += 1
        if URGENT_RE.search(text):
            score += 2
        if TIME_RE.search(text):
            score += 1
        if item.source == "gmail":
            if is_promotional(item):
                score -= 3
            elif EMAIL_ASK_RE.search(text):
                score += 1
            if known:
                score += 1
        return score, vip

    # ---- threads / channels -------------------------------------------------------------------------
    async def thread(self, ch: Channel, limit: int = 15) -> list[ThreadMsg]:
        try:
            if ch.app == "whatsapp" and "whatsapp" in self.readers:
                msgs = await asyncio.to_thread(self.readers["whatsapp"].thread, ch.ident, limit)
                return [ThreadMsg(m.from_me, m.text, m.when) for m in msgs]
            if ch.app == "imessage" and "imessage" in self.readers:
                msgs = await asyncio.to_thread(self.readers["imessage"].thread, ch.ident, limit)
                return [ThreadMsg(m.from_me, m.text, m.when) for m in msgs]
            if ch.app == "gmail" and "gmail" in self.readers and ch.thread_id:
                msgs = await self.readers["gmail"].thread(ch.thread_id, min(limit, 6))
                return [ThreadMsg(False, f"{m['item'].sender}: {m['text'][:1200]}", m["item"].timestamp) for m in msgs]
        except Exception as e:
            log.warning("thread read failed for %s: %r", ch.app, e)
        return []

    async def sent_samples(self, ch: Channel, n: int) -> list[str]:
        try:
            if ch.app == "whatsapp" and "whatsapp" in self.readers:
                return await asyncio.to_thread(self.readers["whatsapp"].sent_samples, ch.ident, n)
            if ch.app == "imessage" and "imessage" in self.readers:
                return await asyncio.to_thread(self.readers["imessage"].sent_samples, ch.ident, n)
        except Exception:
            log.debug("sent samples failed", exc_info=True)
        return []

    def channels_for(self, contact: Contact) -> list[Channel]:
        out: list[Channel] = []
        phone = contact.phones[0] if contact.phones else ""
        if "whatsapp" in self.readers and (contact.wa_jid or phone):
            from jarvis_core.comms.whatsapp_db import phone_to_jid

            out.append(Channel("whatsapp", contact.wa_jid or phone_to_jid(phone), contact.name, phone=phone,
                               thread_id=contact.wa_jid, is_group=contact.wa_jid.endswith("@g.us")))
        if "imessage" in self.readers and (contact.handles or phone or contact.emails):
            handle = contact.handles[0] if contact.handles else (phone or contact.emails[0])
            out.append(Channel("imessage", handle, contact.name, phone=phone, email=contact.emails[0] if contact.emails else ""))
        if "gmail" in self.readers and contact.emails:
            out.append(Channel("gmail", contact.emails[0], contact.name, email=contact.emails[0]))
        return out

    async def pick_channel(self, contact: Contact, app: str | None = None) -> Channel | None:
        channels = self.channels_for(contact)
        if app:
            return next((c for c in channels if c.app == app), self._manual_channel(contact, app))
        if not channels:
            return None
        if len(channels) == 1:
            return channels[0]
        chat_channels = [c for c in channels if c.app in ("whatsapp", "imessage")] or channels
        latest: tuple[float, Channel] | None = None
        for ch in chat_channels:
            msgs = await self.thread(ch, 1)
            ts = msgs[-1].when.timestamp() if msgs and msgs[-1].when else 0.0
            if latest is None or ts > latest[0]:
                latest = (ts, ch)
        return latest[1] if latest else chat_channels[0]

    def _manual_channel(self, contact: Contact, app: str) -> Channel | None:
        phone = contact.phones[0] if contact.phones else ""
        if app == "linkedin":
            return Channel("linkedin", contact.name, contact.name)
        if app == "gmail" and contact.emails:
            return Channel("gmail", contact.emails[0], contact.name, email=contact.emails[0])
        if app == "whatsapp" and phone:
            from jarvis_core.comms.whatsapp_db import phone_to_jid

            return Channel("whatsapp", phone_to_jid(phone), contact.name, phone=phone)
        if app == "imessage" and (phone or contact.emails):
            return Channel("imessage", phone or contact.emails[0], contact.name, phone=phone)
        return None

    def channel_for_item(self, item: Item) -> Channel:
        """The channel an incoming item arrived on (for 'reply to that')."""
        if item.source == "whatsapp":
            return Channel("whatsapp", item.thread_id or item.sender_id, item.meta.get("chat") or item.sender,
                           thread_id=item.thread_id, phone=item.meta.get("phone", ""), is_group=item.is_group)
        if item.source == "imessage":
            handle = item.meta.get("handle") or item.sender_id
            phone = normalise_phone(handle, self.cfg.messaging.default_country_code) if "@" not in handle else ""
            return Channel("imessage", handle, item.sender, phone=phone, email=handle if "@" in handle else "", is_group=item.is_group)
        if item.source == "gmail":
            return Channel("gmail", item.sender_id, item.sender, thread_id=item.thread_id, email=item.sender_id,
                           subject=item.title, in_reply_to=item.meta.get("message_id", ""), references=item.meta.get("references", ""))
        return Channel(item.source, item.sender, item.sender)

    def find_item(self, source: str, item_id: str) -> Item | None:
        for it in self._cache.get(source, (0.0, []))[1]:
            if it.id == item_id:
                return it
        return None

    async def refresh_contacts(self) -> None:
        """Rebuild the contact book from Apple Contacts, WhatsApp chats and iMessage handles."""
        try:
            from jarvis_core.adapters import services as bridge

            records = await asyncio.to_thread(bridge.apple_contacts)
            self.contacts.load_apple(records)
        except Exception as e:
            log.info("Apple Contacts unavailable: %r", e)
        wa = self.readers.get("whatsapp")
        if wa is not None and wa.available():
            self.contacts.load_whatsapp(await asyncio.to_thread(wa.chats))
        im = self.readers.get("imessage")
        if im is not None and im.available():
            self.contacts.load_imessage_handles(await asyncio.to_thread(im.handles))

    def last_item_from(self, name: str, source: str | None = None) -> Item | None:
        want = name.lower()
        for src, (_, items) in self._cache.items():
            if source and src != source:
                continue
            for it in items:
                if want in it.sender.lower():
                    return it
        return None
