"""Name -> phone / email / handle resolution (Apple Contacts + chat names + aliases)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from rapidfuzz import fuzz, process

log = logging.getLogger(__name__)

PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s\-().]{7,}\d")
EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
FUZZY_THRESHOLD = 86


def normalise_phone(raw: str, default_cc: str = "91") -> str:
    """Best-effort E.164. Returns '' when it doesn't look like a phone number."""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if raw.startswith("00"):
        return "+" + digits[2:]
    if len(digits) == 10:
        return f"+{default_cc}{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+{default_cc}{digits[1:]}"
    return "+" + digits


def _key(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", name.lower())).strip()


@dataclass
class Contact:
    name: str
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)  # iMessage handles
    wa_jid: str = ""
    sources: set[str] = field(default_factory=set)
    last_contact: datetime | None = None
    is_group: bool = False

    def identifiers(self) -> list[str]:
        return [*self.phones, *self.emails, *self.handles, *([self.wa_jid] if self.wa_jid else [])]


@dataclass
class Resolution:
    candidates: list[Contact]
    ambiguous: bool = False

    @property
    def best(self) -> Contact | None:
        return self.candidates[0] if self.candidates else None

    def spoken_choices(self) -> str:
        names = [c.name for c in self.candidates[:3]]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


class ContactBook:
    def __init__(self, default_cc: str = "91", aliases: Callable[[], dict[str, str]] | None = None) -> None:
        self.default_cc = default_cc
        self._aliases = aliases or (lambda: {})
        self._by_key: dict[str, list[Contact]] = {}

    # -- building --------------------------------------------------------------------------------
    @property
    def contacts(self) -> list[Contact]:
        return [c for group in self._by_key.values() for c in group]

    def _find(self, name: str, ids: list[str]) -> Contact | None:
        # same phone/email -> same person, regardless of how the name is spelled
        for c in self.contacts:
            if any(i and i in c.identifiers() for i in ids):
                return c
        group = self._by_key.get(_key(name))
        return group[0] if group else None

    def add(self, name: str, *, phones: list[str] | None = None, emails: list[str] | None = None,
            handles: list[str] | None = None, wa_jid: str = "", source: str = "", last_contact: datetime | None = None,
            is_group: bool = False) -> Contact:
        name = name.strip()
        ph = [p for p in (normalise_phone(x, self.default_cc) for x in (phones or [])) if p]
        em = [e.lower() for e in (emails or [])]
        existing = self._find(name, [*ph, *em, *(handles or []), wa_jid])
        if existing is None:
            existing = Contact(name=name, is_group=is_group)
            self._by_key.setdefault(_key(name), []).append(existing)
        for p in ph:
            if p not in existing.phones:
                existing.phones.append(p)
        for e in em:
            if e not in existing.emails:
                existing.emails.append(e)
        for h in handles or []:
            if h and h not in existing.handles:
                existing.handles.append(h)
        if wa_jid:
            existing.wa_jid = wa_jid
        if source:
            existing.sources.add(source)
        if last_contact and (existing.last_contact is None or last_contact > existing.last_contact):
            existing.last_contact = last_contact
        return existing

    def load_apple(self, records: list[dict[str, Any]]) -> int:
        n = 0
        for r in records:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            blob = " ".join([str(r.get("content") or ""), *map(str, (r.get("metadata") or {}).values()), *map(str, r.get("participants") or [])])
            self.add(name, phones=PHONE_RE.findall(blob), emails=EMAIL_RE.findall(blob), source="contacts")
            n += 1
        return n

    def load_whatsapp(self, chats: list[Any]) -> None:
        for c in chats:
            if c.is_group:
                self.add(c.name, wa_jid=c.jid, source="whatsapp", last_contact=c.last_message, is_group=True)
            else:
                self.add(c.name, phones=[c.phone] if c.phone else [], wa_jid=c.jid, source="whatsapp", last_contact=c.last_message)

    def load_imessage_handles(self, handles: list[str]) -> None:
        for h in handles:
            # attach to a known contact when the number/email matches, otherwise keep a bare entry
            if "@" in h:
                known = self._find("", [h.lower()])
                if known:
                    self.add(known.name, handles=[h], emails=[h], source="imessage")
            else:
                phone = normalise_phone(h, self.default_cc)
                known = self._find("", [phone]) if phone else None
                if known:
                    self.add(known.name, handles=[h], source="imessage")

    # -- lookups -----------------------------------------------------------------------------------
    def by_identifier(self, ident: str) -> Contact | None:
        if not ident:
            return None
        phone = normalise_phone(ident, self.default_cc) if "@" not in ident else ""
        for c in self.contacts:
            ids = c.identifiers()
            if ident in ids or ident.lower() in ids or (phone and phone in ids):
                return c
        return None

    def name_for(self, ident: str) -> str:
        c = self.by_identifier(ident)
        return c.name if c else ""

    def resolve(self, query: str) -> Resolution:
        q = _key(query)
        if not q:
            return Resolution([])
        # 1. aliases ("my brother" -> Arjun)
        for alias, target in self._aliases().items():
            if _key(alias) in (q, re.sub(r"^my ", "", q)):
                inner = self.resolve(target) if _key(target) != q else Resolution([])
                if inner.candidates:
                    return inner
        everyone = [c for c in self.contacts if not c.is_group] or self.contacts
        # 2. exact full name
        exact = [c for c in everyone if _key(c.name) == q]
        if exact:
            return Resolution(self._rank(exact), ambiguous=len(exact) > 1)
        # 3. every word you said appears in the name ("rahul" -> Rahul Sharma, Rahul Verma)
        q_tokens = set(q.split())
        subset = [c for c in everyone if q_tokens <= set(_key(c.name).split())]
        if subset:
            ranked = self._rank(subset)
            return Resolution(ranked, ambiguous=len(ranked) > 1 and not self._clear_favourite(ranked))
        # 4. fuzzy
        names = {c.name: c for c in everyone}
        hits = process.extract(query, list(names), scorer=fuzz.WRatio, processor=str.lower, limit=4, score_cutoff=FUZZY_THRESHOLD)
        if not hits:
            return Resolution([])
        top = hits[0][1]
        close = [names[n] for n, s, _ in hits if top - s <= 3]
        ranked = self._rank(close)
        return Resolution(ranked, ambiguous=len(ranked) > 1)

    @staticmethod
    def _rank(cs: list[Contact]) -> list[Contact]:
        def key(c: Contact):
            ts = c.last_contact.timestamp() if c.last_contact else 0
            return (-len(c.sources & {"whatsapp", "imessage"}), -ts)

        return sorted(cs, key=key)

    @staticmethod
    def _clear_favourite(ranked: list[Contact]) -> bool:
        """One candidate you actually chat with, the rest never messaged."""
        active = [c for c in ranked if c.sources & {"whatsapp", "imessage"}]
        return len(active) == 1
