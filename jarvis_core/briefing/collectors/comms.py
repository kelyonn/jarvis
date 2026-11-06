"""Email and message sections, backed by the comms hub (built in the messaging phase)."""

from __future__ import annotations

from datetime import datetime, timedelta

from jarvis_core.briefing.env import Env
from jarvis_core.briefing.models import MailItem

EMAIL_SOURCES = ["gmail"]
MESSAGE_SOURCES = ["whatsapp", "imessage"]


async def collect(env: Env, kind: str, since: datetime | None = None) -> list[MailItem]:
    hub = env.services.get("comms")
    if hub is None:
        raise RuntimeError("messaging is not set up")
    sources = EMAIL_SOURCES if kind == "email" else list(MESSAGE_SOURCES)
    if kind == "messages" and env.cfg.briefing.include_linkedin:
        sources.append("linkedin")
    since = since or (env.now() - timedelta(hours=18))
    items = await hub.unread(since=since, sources=sources)
    out: list[MailItem] = []
    for it in items:
        score, vip = hub.importance(it)
        out.append(
            MailItem(
                id=f"{it.source}:{it.id}",
                sender=it.sender or "someone",
                subject=it.title or it.body[:60],
                snippet=(it.body or "")[:200],
                important=score >= 2 or vip,
                vip=vip,
                timestamp=it.timestamp,
            )
        )
    out.sort(key=lambda m: (not m.vip, not m.important, -(m.timestamp.timestamp() if m.timestamp else 0)))
    return out


def describe(items: list[MailItem], noun: str) -> str:
    if not items:
        return f"No new {noun}."
    vips = [m for m in items if m.vip]
    imp = [m for m in items if m.important and not m.vip]
    s = f"{len(items)} new {noun}"
    bits = []
    if vips:
        bits.append("from " + ", ".join(dict.fromkeys(m.sender for m in vips[:3])))
    if imp:
        bits.append("worth a look: " + "; ".join(f"{m.sender} about {m.subject}" for m in imp[:2]))
    return s + (" (" + "; ".join(bits) + ")" if bits else "") + "."
