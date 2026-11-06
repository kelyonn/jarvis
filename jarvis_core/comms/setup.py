"""Assemble the messaging subsystem and register it in the tool context."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis_core.briefing.store import Store
from jarvis_core.comms.contacts import ContactBook
from jarvis_core.comms.drafter import Drafter
from jarvis_core.comms.gmail import GmailClient
from jarvis_core.comms.hub import CommsHub
from jarvis_core.comms.imessage import IMessageDB
from jarvis_core.comms.insert import Inserter
from jarvis_core.comms.linkedin import LinkedInReader
from jarvis_core.comms.offers import OfferPolicy
from jarvis_core.comms.vip import VipList
from jarvis_core.comms.whatsapp_db import WhatsAppDB
from jarvis_core.config import Config
from jarvis_core.skills.base import ToolContext

log = logging.getLogger(__name__)

CONTACT_REFRESH_S = 3600.0


def memory_aliases(ctx: ToolContext):
    """Aliases like 'my brother' -> 'Arjun', stored as person facts by the memory system."""

    def provider() -> dict[str, str]:
        mem = ctx.services.get("memory")
        if mem is None:
            return {}
        try:
            return {f.meta["alias"]: f.meta["name"] for f in mem.list("person") if f.meta.get("alias") and f.meta.get("name")}
        except Exception:
            return {}

    return provider


def build_comms(cfg: Config, ctx: ToolContext, store: Store, llm: Any = None, *, gmail_session: Any = None) -> CommsHub:
    apps = set(cfg.messaging.apps)
    whatsapp = WhatsAppDB(cfg.messaging.whatsapp_db or None) if "whatsapp" in apps else None
    imessage = IMessageDB(cfg.messaging.imessage_db or None) if "imessage" in apps else None
    linkedin = LinkedInReader(ctx) if "linkedin" in apps else None
    gmail = None
    missing: dict[str, str] = {}
    if "gmail" in apps:
        try:
            session = gmail_session
            if session is None:
                from jarvis_core.adapters.services import GoogleSession

                session = GoogleSession("gmail")
            gmail = GmailClient(session)
        except Exception as e:
            missing["gmail"] = f"Google sign-in support is unavailable ({e.__class__.__name__})"

    contacts = ContactBook(cfg.messaging.default_country_code, aliases=memory_aliases(ctx))
    vip = VipList(cfg)
    hub = CommsHub(cfg, contacts=contacts, vip=vip, whatsapp=whatsapp, imessage=imessage, gmail=gmail, linkedin=linkedin)
    hub.missing = missing
    ctx.services.update(
        comms=hub, contacts=contacts, vip=vip, offers=OfferPolicy(store, cfg.messaging),
        drafter=Drafter(llm, user_name=cfg.user.name, services=ctx.services),
        inserter=Inserter(ctx, gmail),
    )
    return hub


class ContactRefresher:
    """Keeps the contact book fresh: once at start-up and then hourly."""

    def __init__(self, hub: CommsHub) -> None:
        self.hub = hub
        self._task: asyncio.Task | None = None

    async def on_ready(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="contact-refresh")

    async def _loop(self) -> None:
        while True:
            try:
                await self.hub.refresh_contacts()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("contact refresh failed")
            await asyncio.sleep(CONTACT_REFRESH_S)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
