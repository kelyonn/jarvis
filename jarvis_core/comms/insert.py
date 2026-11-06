"""Put a draft where the user can review it. Nothing in this module can send a message:

* WhatsApp / iMessage: URL schemes that open a pre-filled compose box.
* Gmail: ``users.drafts.create`` (the Gmail client has no send call).
* LinkedIn / WhatsApp groups: type into the compose field via accessibility, or paste with Cmd+V.
* The only key events ever synthesized here are Cmd+V (and Cmd+F to search WhatsApp), and each goes
  through :class:`ActionFilter` first.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

from jarvis_core.comms import axutil
from jarvis_core.comms.drafter import APP_LABEL, Draft
from jarvis_core.comms.gmail import GmailClient
from jarvis_core.comms.linkedin import BROWSER, MESSAGING_URL
from jarvis_core.errors import SkillError
from jarvis_core.policy.action_filter import ActionFilter
from jarvis_core.skills.base import ToolContext

log = logging.getLogger(__name__)

MAX_URL_TEXT = 1800


@dataclass
class InsertResult:
    app: str
    method: str  # url | gmail_draft | ax | paste
    message: str
    url: str = ""


class Inserter:
    def __init__(self, ctx: ToolContext, gmail: GmailClient | None = None, *, settle_s: float = 0.8, page_wait_s: float = 3.0) -> None:
        self.ctx = ctx
        self.gmail = gmail
        self.filter = ActionFilter(ctx.cfg.safety)
        self.settle = settle_s
        self.page_wait = page_wait_s

    # ---- public ------------------------------------------------------------------------------------
    async def insert(self, draft: Draft) -> InsertResult:
        ch = draft.channel
        label = APP_LABEL.get(ch.app, ch.app)
        if ch.app == "whatsapp":
            res = await self._whatsapp(draft)
        elif ch.app == "imessage":
            res = await self._imessage(draft)
        elif ch.app == "gmail":
            res = await self._gmail(draft)
        elif ch.app == "linkedin":
            res = await self._linkedin(draft)
        else:
            raise SkillError(f"I don't know how to draft into {ch.app}, sir.")
        draft.inserted = True
        self.ctx.audit.write("draft_inserted", app=ch.app, to=ch.name, method=res.method, chars=len(draft.body))
        res.message = res.message or f"The draft is ready in {label}, sir. Over to you."
        return res

    async def open_chat(self, ch) -> str:
        """Bring up the conversation with this person, without putting any text in the compose box."""
        label = APP_LABEL.get(ch.app, ch.app)
        if ch.app == "whatsapp":
            digits = re.sub(r"\D", "", ch.phone)
            if digits and not ch.is_group:
                await self._open(f"whatsapp://send?phone={digits}")
            else:
                await self._open_app("WhatsApp")
                await asyncio.sleep(self.settle)
                await self._key("f", ["cmd"], "WhatsApp")
                await self.ctx.shell_request("input.type", {"text": ch.name})
                await asyncio.sleep(self.settle)
                row = axutil.find_row(await self._tree("WhatsApp"), ch.name)
                if row is None:
                    raise SkillError(f"I couldn't find the {ch.name} chat in WhatsApp, sir.")
                await self.ctx.shell_request("ax.press", {"element_id": row["id"], "label": axutil.label(row)})
        elif ch.app == "imessage":
            await self._open(f"sms:{ch.phone or ch.email or ch.ident}")
        else:
            raise SkillError("I can only open WhatsApp and iMessage chats, sir.")
        self.ctx.audit.write("chat_opened", app=ch.app, to=ch.name)
        return f"Opened your {label} chat with {ch.name}, sir."

    # ---- helpers -------------------------------------------------------------------------------------
    async def _open(self, target: str, app: str | None = None) -> None:
        argv = ["open", *(["-a", app] if app else []), target]
        res = await self.ctx.run(argv)
        if not res.ok:
            raise SkillError(f"I couldn't open that, sir. {res.err[:80]}")

    async def _key(self, key: str, modifiers: list[str], app: str, focused_role: str = "") -> None:
        verdict = self.filter.check_key(key, modifiers, app=app, focused_role=focused_role)
        if not verdict:
            raise SkillError(verdict.reason)
        await self.ctx.shell_request("input.key", {"key": key, "modifiers": modifiers})

    async def _tree(self, app: str) -> axutil.Node | None:
        data = await self.ctx.shell_request("ax.tree", {"app": app, "max_depth": 14, "max_nodes": 900}, timeout=15)
        return data.get("tree")

    async def _paste(self, text: str, app: str, compose: axutil.Node | None = None) -> None:
        """Clipboard + Cmd+V, restoring whatever was on the clipboard."""
        old = await self.ctx.run(["pbpaste"])
        await self.ctx.run(["pbcopy"], input=text)
        try:
            if compose and compose.get("frame"):
                x, y, w, h = compose["frame"]
                if self.filter.check_click(axutil.label(compose)):
                    await self.ctx.shell_request("input.click", {"x": x + w / 2, "y": y + h / 2, "label": axutil.label(compose)})
                    await asyncio.sleep(0.2)
            await self._key("v", ["cmd"], app, focused_role="AXTextArea")
            await asyncio.sleep(0.2)
        finally:
            await self.ctx.run(["pbcopy"], input=old.out if old.ok else "")

    async def _fill_compose(self, app: str, text: str) -> str:
        """Fill the compose field of the front window. Returns 'ax' or 'paste'."""
        tree = await self._tree(app)
        compose = axutil.find_compose(tree)
        if compose is None:
            raise SkillError(f"I couldn't find the message box in {app}, sir.")
        try:
            await self.ctx.shell_request("ax.set_value", {"element_id": compose["id"], "text": text})
            await asyncio.sleep(0.2)
            check = axutil.find_compose(await self._tree(app))
            if check is not None and text[:15] in str(check.get("value", "")):
                return "ax"
        except SkillError:
            pass
        await self._paste(text, app, compose)
        return "paste"

    async def _prefilled(self, app: str, text: str) -> bool:
        try:
            tree = await self._tree(app)
        except SkillError:
            return True  # can't verify without the shell; trust the URL scheme
        compose = axutil.find_compose(tree)
        return bool(compose and text[:15] in str(compose.get("value", "")))

    # ---- per app ----------------------------------------------------------------------------------------
    async def _whatsapp(self, d: Draft) -> InsertResult:
        ch = d.channel
        digits = re.sub(r"\D", "", ch.phone)
        if digits and not ch.is_group and len(d.body) <= MAX_URL_TEXT:
            await self._open(f"whatsapp://send?phone={digits}&text={quote(d.body, safe='')}")
            return InsertResult("whatsapp", "url", "", f"whatsapp://send?phone={digits}")
        # group chat (or very long text): search for the chat, open it, fill the box
        await self._open_app("WhatsApp")
        await asyncio.sleep(self.settle)
        await self._key("f", ["cmd"], "WhatsApp")
        await self.ctx.shell_request("input.type", {"text": ch.name})
        await asyncio.sleep(self.settle)
        row = axutil.find_row(await self._tree("WhatsApp"), ch.name)
        if row is None:
            raise SkillError(f"I couldn't find the {ch.name} chat in WhatsApp, sir.")
        await self.ctx.shell_request("ax.press", {"element_id": row["id"], "label": axutil.label(row)})
        await asyncio.sleep(self.settle)
        method = await self._fill_compose("WhatsApp", d.body)
        return InsertResult("whatsapp", method, "")

    async def _open_app(self, name: str) -> None:
        res = await self.ctx.run(["open", "-a", name])
        if not res.ok:
            raise SkillError(f"I couldn't open {name}, sir.")

    async def _imessage(self, d: Draft) -> InsertResult:
        ch = d.channel
        handle = ch.phone or ch.email or ch.ident
        await self._open(f"sms:{handle}&body={quote(d.body, safe='')}")
        await asyncio.sleep(self.settle + 0.4)
        if await self._prefilled("Messages", d.body):
            return InsertResult("imessage", "url", "")
        method = await self._fill_compose("Messages", d.body)
        return InsertResult("imessage", method, "")

    async def _gmail(self, d: Draft) -> InsertResult:
        if self.gmail is None:
            raise SkillError("Gmail isn't connected, sir.")
        ch = d.channel
        subject = d.subject or ch.subject or "Quick note"
        if ch.thread_id and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        res = await self.gmail.create_draft(
            to=ch.email or ch.ident, subject=subject, body=d.body, thread_id=ch.thread_id or None,
            in_reply_to=ch.in_reply_to, references=ch.references,
        )
        await self._open(res["url"])
        return InsertResult("gmail", "gmail_draft", "The draft is saved in Gmail, sir. Over to you.", res["url"])

    async def _linkedin(self, d: Draft) -> InsertResult:
        await self._open(MESSAGING_URL, BROWSER)
        await asyncio.sleep(self.page_wait)
        row = axutil.find_row(await self._tree(BROWSER), d.channel.name)
        if row is None:
            raise SkillError(f"I couldn't find your LinkedIn conversation with {d.channel.name}, sir.")
        await self.ctx.shell_request("ax.press", {"element_id": row["id"], "label": axutil.label(row)})
        await asyncio.sleep(self.page_wait * 0.66)
        method = await self._fill_compose(BROWSER, d.body)
        return InsertResult("linkedin", method, "The draft is in the LinkedIn message box, sir. Over to you.")
