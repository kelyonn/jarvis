"""Gmail: read unread mail and create *drafts*. Nothing here can send mail.

Only ``users.messages.list/get``, ``users.threads.get`` and ``users.drafts.create`` are used, and the
OAuth scope needed is ``gmail.modify`` (never ``gmail.send``).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Protocol

from jarvis_core.errors import SkillError
from jarvis_core.interfaces import Item

log = logging.getLogger(__name__)

API = "https://gmail.googleapis.com/gmail/v1/users/me"
DRAFT_SCOPES = ("gmail.modify", "gmail.compose")
PROMO_HINTS = ("unsubscribe", "no-reply", "noreply", "newsletter", "do-not-reply", "notifications@", "marketing")


class Session(Protocol):
    def connected(self) -> bool: ...
    def scopes(self) -> str: ...
    def request(self, method: str, url: str, **kw: Any): ...


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (msg.get("payload", {}).get("headers") or [])}


def _body_text(payload: dict[str, Any]) -> str:
    """Best-effort plain-text body from a Gmail message payload."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if mime == "text/plain" and data:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    for part in payload.get("parts") or []:
        text = _body_text(part)
        if text:
            return text
    return ""


def parse_message(msg: dict[str, Any]) -> Item:
    h = _headers(msg)
    name, addr = parseaddr(h.get("from", ""))
    when = None
    if "date" in h:
        try:
            when = parsedate_to_datetime(h["date"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            when = None
    if when is None and msg.get("internalDate"):
        when = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    return Item(
        "gmail", msg["id"], sender=name or addr, sender_id=addr.lower(), title=h.get("subject", "(no subject)"),
        body=(msg.get("snippet") or "").strip(), timestamp=when, thread_id=msg.get("threadId", ""),
        unread="UNREAD" in (msg.get("labelIds") or []),
        meta={"message_id": h.get("message-id", ""), "references": h.get("references", ""), "reply_to": h.get("reply-to", ""),
              "to": h.get("to", ""), "labels": msg.get("labelIds") or [], "list_unsubscribe": "list-unsubscribe" in h},
    )


def is_promotional(item: Item) -> bool:
    hay = f"{item.sender_id} {item.sender}".lower()
    return bool(item.meta.get("list_unsubscribe")) or any(h in hay for h in PROMO_HINTS)


def build_raw_message(*, to: str, subject: str, body: str, in_reply_to: str = "", references: str = "", sender: str = "") -> str:
    m = EmailMessage()
    m["To"] = to
    m["Subject"] = subject
    if sender:
        m["From"] = sender
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = f"{references} {in_reply_to}".strip()
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


class GmailClient:
    name = "gmail"

    def __init__(self, session: Session) -> None:
        self.session = session
        self.disabled_reason = ""

    def check(self) -> tuple[bool, str]:
        try:
            if not self.session.connected():
                self.disabled_reason = "Gmail isn't connected (run `jarvisd setup` to sign in with Google)"
                return False, self.disabled_reason
        except Exception as e:
            self.disabled_reason = f"Gmail credentials unavailable ({e})"
            return False, self.disabled_reason
        self.disabled_reason = ""
        return True, ""

    def available(self) -> bool:
        return self.check()[0]

    def can_draft(self) -> bool:
        try:
            scopes = self.session.scopes()
        except Exception:
            return False
        return any(s in scopes for s in DRAFT_SCOPES)

    # -- sync helpers (run in a worker thread) ------------------------------------------------
    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = self.session.request("GET", f"{API}/{path}", params=params)
        if r.status_code >= 400:
            raise SkillError(f"Gmail said {r.status_code}, sir.")
        return r.json()

    def _unread_sync(self, since: datetime | None, max_items: int) -> list[Item]:
        q = "is:unread in:inbox -category:promotions -category:social"
        if since:
            q += f" after:{int(since.timestamp())}"
        listing = self._get("messages", q=q, maxResults=max_items)
        out = []
        for ref in listing.get("messages", []):
            msg = self._get(f"messages/{ref['id']}", format="metadata",
                            metadataHeaders=["From", "Subject", "Date", "Message-ID", "References", "To", "Reply-To", "List-Unsubscribe"])
            out.append(parse_message(msg))
        return out

    def _thread_sync(self, thread_id: str, limit: int) -> list[dict[str, Any]]:
        data = self._get(f"threads/{thread_id}", format="full")
        out = []
        for msg in data.get("messages", [])[-limit:]:
            item = parse_message(msg)
            out.append({"item": item, "text": (_body_text(msg.get("payload", {})) or item.body).strip()})
        return out

    def _draft_sync(self, raw: str, thread_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id
        r = self.session.request("POST", f"{API}/drafts", json=body)
        if r.status_code >= 400:
            raise SkillError(f"Gmail refused the draft ({r.status_code}), sir.")
        return r.json()

    # -- async API ---------------------------------------------------------------------------------
    async def unread(self, since: datetime | None = None, max_items: int = 15) -> list[Item]:
        return await asyncio.to_thread(self._unread_sync, since, max_items)

    async def thread(self, thread_id: str, limit: int = 6) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._thread_sync, thread_id, limit)

    async def search(self, query: str, max_items: int = 8) -> list[Item]:
        def run() -> list[Item]:
            listing = self._get("messages", q=query, maxResults=max_items)
            return [parse_message(self._get(f"messages/{r['id']}", format="metadata",
                                            metadataHeaders=["From", "Subject", "Date", "Message-ID"]))
                    for r in listing.get("messages", [])]

        return await asyncio.to_thread(run)

    async def create_draft(
        self, *, to: str, subject: str, body: str, thread_id: str | None = None, in_reply_to: str = "", references: str = ""
    ) -> dict[str, str]:
        if not self.can_draft():
            raise SkillError("Gmail drafts need the modify permission; please reconnect Google, sir.")
        raw = build_raw_message(to=to, subject=subject, body=body, in_reply_to=in_reply_to, references=references)
        res = await asyncio.to_thread(self._draft_sync, raw, thread_id)
        msg_id = (res.get("message") or {}).get("id", "")
        return {"draft_id": res.get("id", ""), "message_id": msg_id, "url": compose_url(msg_id or res.get("id", ""))}


def compose_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}"
