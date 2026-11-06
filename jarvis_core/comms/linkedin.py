"""LinkedIn messages. There is no API, so this is best effort: it opens the messaging page in Chrome and
reads the accessibility tree (or, if available, asks the screen-vision service). Clearly labelled
best-effort in everything it says."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone

from jarvis_core.comms import axutil
from jarvis_core.errors import SkillError
from jarvis_core.interfaces import Item
from jarvis_core.skills.base import ToolContext

log = logging.getLogger(__name__)

MESSAGING_URL = "https://www.linkedin.com/messaging/"
BROWSER = "Google Chrome"
UNREAD_RE = re.compile(r"\bunread\b|\bnew message\b", re.I)


def _item(name: str, preview: str) -> Item:
    digest = hashlib.sha1(f"{name}|{preview}".encode()).hexdigest()[:12]
    return Item("linkedin", digest, sender=name, sender_id=name.lower(), body=preview, timestamp=datetime.now(timezone.utc),
                thread_id=name.lower())


def parse_threads(tree: axutil.Node | None, *, unread_only: bool = True) -> list[Item]:
    """Pull conversation rows out of the messaging page. Rows read like 'Priya Shah  Status is online  Hi, are you ...'."""
    items: list[Item] = []
    for n in axutil.walk(tree):
        if n.get("role") not in {"AXRow", "AXLink", "AXGroup", "AXCell", "AXOutlineRow"}:
            continue
        text = axutil.label(n)
        if len(text) < 12 or len(text) > 400:
            continue
        if unread_only and not UNREAD_RE.search(text):
            continue
        cleaned = UNREAD_RE.sub("", text)
        cleaned = re.sub(r"\b(?:Status is (?:online|offline|reachable)|Active now)\b\.?", "", cleaned, flags=re.I)
        parts = [p.strip(" .,-") for p in re.split(r"\s{2,}|\n|\s·\s", cleaned) if p.strip(" .,-")]
        if not parts:
            continue
        name, preview = parts[0], " ".join(parts[1:])[:200]
        if 1 < len(name) < 60 and not any(i.sender == name for i in items):
            items.append(_item(name, preview))
    return items


def parse_vision_lines(text: str) -> list[Item]:
    """'Name: preview' lines from the vision model."""
    out = []
    for line in text.splitlines():
        line = line.strip(" -*•\t")
        if ":" not in line or len(line) < 5:
            continue
        name, preview = line.split(":", 1)
        if 1 < len(name.strip()) < 60 and "no unread" not in line.lower():
            out.append(_item(name.strip(), preview.strip()[:200]))
    return out


class LinkedInReader:
    name = "linkedin"

    def __init__(self, ctx: ToolContext, page_wait_s: float = 3.0) -> None:
        self.ctx = ctx
        self.page_wait = page_wait_s
        self.disabled_reason = ""

    def check(self) -> tuple[bool, str]:
        fe = self.ctx.frontend
        if fe is None or not getattr(fe, "connected", False):
            self.disabled_reason = "LinkedIn needs the Jarvis shell app (screen access)"
            return False, self.disabled_reason
        return True, ""

    def available(self) -> bool:
        return self.check()[0]

    async def open_messaging(self) -> None:
        res = await self.ctx.run(["open", "-a", BROWSER, MESSAGING_URL])
        if not res.ok:
            raise SkillError("I couldn't open Chrome for LinkedIn, sir.")
        await asyncio.sleep(self.page_wait)

    async def tree(self) -> axutil.Node | None:
        data = await self.ctx.shell_request("ax.tree", {"app": BROWSER, "max_depth": 14, "max_nodes": 900}, timeout=15)
        return data.get("tree")

    async def unread(self, since: datetime | None = None, max_items: int = 10) -> list[Item]:
        await self.open_messaging()
        items = parse_threads(await self.tree())
        if not items:
            vision = self.ctx.services.get("vision")
            if vision is not None:
                try:
                    text = await vision.describe(
                        "This is LinkedIn messaging. List each UNREAD conversation as one line 'Sender name: message preview'. "
                        "If none are unread, say 'no unread'."
                    )
                    items = parse_vision_lines(text)
                except Exception as e:
                    log.debug("linkedin vision fallback failed: %r", e)
        return items[:max_items]
