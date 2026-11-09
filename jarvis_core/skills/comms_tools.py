"""Messaging tools: read, draft, revise, insert (never send), VIPs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from jarvis_core.comms.drafter import APP_LABEL, Draft
from jarvis_core.comms.hub import Channel
from jarvis_core.errors import SkillError
from jarvis_core.interfaces import Item
from jarvis_core.memory.style import learn_style
from jarvis_core.skills.base import E, I, S, Tool, ToolContext, ToolRegistry, obj

APPS = ["whatsapp", "imessage", "gmail", "linkedin"]
DRAFT_KEY = "draft"


def _svc(ctx: ToolContext, name: str):
    s = ctx.services.get(name)
    if s is None:
        raise SkillError("Messaging isn't set up yet, sir. Run `jarvisd setup`.")
    return s


DETAIL_MARK = "\n\n[details]\n"


def _shorten(text: str, n: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def summarise_items(items: list[Item], limit: int = 6) -> str:
    if not items:
        return "Nothing new, sir."
    per_chat: dict[str, list[Item]] = {}
    for it in items:
        per_chat.setdefault(f"{it.source}:{it.thread_id or it.sender}", []).append(it)
    parts = []
    for group in list(per_chat.values())[:limit]:
        first = group[0]
        where = {"whatsapp": "on WhatsApp", "imessage": "by iMessage", "gmail": "by email", "linkedin": "on LinkedIn"}.get(first.source, "")
        if first.source == "gmail":
            parts.append(f"{first.sender}, {where}: {_shorten(first.title, 90)}.")
        elif len(group) == 1:
            parts.append(f"{first.sender}, {where}: {_shorten(first.body)}")
        else:
            newest = group[0]
            parts.append(f"{first.sender}, {where}, {len(group)} messages. The latest: {_shorten(newest.body, 100)}")
    extra = len(per_chat) - limit
    head = f"{len(items)} unread." if len(per_chat) > 1 else ""
    tail = f" And {extra} more conversation{'s' if extra > 1 else ''}." if extra > 0 else ""
    return " ".join(p for p in [head, *parts] if p) + tail


def _when(dt: datetime | None) -> str:
    """Short spoken-friendly time: '2:14 pm', 'yesterday 9:05 am', 'Monday 6:30 pm'."""
    if dt is None:
        return ""
    now = datetime.now().astimezone()
    dt = dt.astimezone()
    clock = dt.strftime("%I:%M %p").lstrip("0").lower()
    days = (now.date() - dt.date()).days
    if days == 0:
        return clock
    if days == 1:
        return f"yesterday {clock}"
    if days < 7:
        return f"{dt.strftime('%A')} {clock}"
    return f"{dt.strftime('%d %B')} {clock}"


def full_messages(items: list[Item], limit: int = 10) -> str:
    """The actual text of unread messages, oldest first, grouped by sender (not just a one-line summary)."""
    items = sorted(items, key=lambda i: i.timestamp or datetime.min.astimezone())[-limit:]
    lines = []
    for it in items:
        where = {"whatsapp": "WhatsApp", "imessage": "iMessage", "gmail": "email"}.get(it.source, it.source)
        body = (it.body or it.title or "").strip()
        if it.source == "gmail" and it.title:
            body = f"{it.title}. {body}"
        lines.append(f"{it.sender} on {where}, {_when(it.timestamp)}: {body[:700]}")
    return "\n".join(lines) if lines else "Nothing new, sir."


async def _read_conversation(ctx: ToolContext, a: dict[str, Any]) -> str:
    """Recent messages with one person (both sides), whether or not they are unread."""
    hub = _svc(ctx, "comms")
    ch = await _resolve_channel(ctx, {"person": a.get("person"), "app": a.get("app")})
    msgs = await hub.thread(ch, max(1, min(int(a.get("limit") or 12), 40)))
    if not msgs:
        return f"I couldn't find any messages with {ch.name} on {APP_LABEL.get(ch.app, ch.app)}, sir."
    lines = [f"{'You' if m.from_me else ch.name} ({_when(m.when)}): {m.text.strip()[:700]}" for m in msgs if m.text.strip()]
    return f"Conversation with {ch.name} on {APP_LABEL.get(ch.app, ch.app)}, oldest first.\n" + "\n".join(lines)


# ---- reading -----------------------------------------------------------------------------------------


async def _read_messages(ctx: ToolContext, a: dict[str, Any]) -> str:
    hub = _svc(ctx, "comms")
    source = a.get("source")
    sources = [source] if source and source != "all" else [s for s in hub.enabled_sources() if s != "linkedin"]
    status = hub.status()
    problems = [f"{s}: {status[s][1]}" for s in sources if s in status and not status[s][0] and status[s][1]]
    items = await hub.unread(since=datetime.now().astimezone() - timedelta(days=3), sources=sources)
    person = (a.get("person") or "").strip().lower()
    if person:
        items = [i for i in items if person in i.sender.lower()]
        if items:
            return full_messages(items, int(a.get("limit") or 10))
    text = summarise_items(items)
    if problems and not items:
        text = "I couldn't read that, sir. " + problems[0]
    elif len(items) > 1:  # the plain summary is the fallback; the detail lets the reply say what people actually want
        text += DETAIL_MARK + full_messages(items, 25)
    return text


async def _search_messages(ctx: ToolContext, a: dict[str, Any]) -> str:
    hub = _svc(ctx, "comms")
    needle = a["query"].strip().lower()
    sources = [s for s in hub.enabled_sources() if s != "linkedin"]
    items = await hub.unread(since=datetime.now().astimezone() - timedelta(days=14), sources=sources)
    hits = [i for i in items if needle in f"{i.sender} {i.title} {i.body}".lower()]
    if not hits:
        return f"Nothing in your recent messages mentions {a['query']}, sir."
    return summarise_items(hits)


async def _check_linkedin(ctx: ToolContext, a: dict[str, Any]) -> str:
    hub = _svc(ctx, "comms")
    if "linkedin" not in hub.readers:
        raise SkillError("LinkedIn isn't enabled, sir.")
    items = await hub.unread(sources=["linkedin"])
    if not items:
        return "I see no unread LinkedIn messages, sir. That's a best-effort check of the page."
    return "Best effort, sir. " + summarise_items(items)


# ---- drafting ----------------------------------------------------------------------------------------


def _offer_channel(ctx: ToolContext, hub) -> tuple[Channel, str] | None:
    offer = ctx.state.get("alert_offer")
    if not offer:
        return None
    item = hub.find_item(offer.get("source", ""), offer.get("item_id", ""))
    if item is None:
        return None
    return hub.channel_for_item(item), item.sender


async def _resolve_channel(ctx: ToolContext, a: dict[str, Any]) -> Channel:
    hub = _svc(ctx, "comms")
    app = a.get("app") if a.get("app") in APPS else None
    person = (a.get("person") or "").strip()
    if not person:
        found = _offer_channel(ctx, hub)
        if not found:
            raise SkillError("Who is it for, sir?")
        return found[0]
    # replying to something that just arrived from this person wins over a fresh conversation
    if a.get("reply") or app == "gmail":
        item = hub.last_item_from(person, app)
        if item is not None:
            return hub.channel_for_item(item)
    res = hub.contacts.resolve(person)
    if not res.candidates:
        raise SkillError(f"I couldn't find anyone called {person} in your contacts, sir.")
    if res.ambiguous:
        raise SkillError(f"{res.spoken_choices()}, sir?")
    ch = await hub.pick_channel(res.best, app)
    if ch is None:
        raise SkillError(f"I have no way to message {res.best.name} on {APP_LABEL.get(app or '', 'any app')}, sir.")
    return ch


async def _draft_message(ctx: ToolContext, a: dict[str, Any]) -> str:
    hub, drafter = _svc(ctx, "comms"), _svc(ctx, "drafter")
    intent = (a.get("message") or "").strip()
    ch = await _resolve_channel(ctx, a)
    thread = await hub.thread(ch, 15)
    samples = await hub.sent_samples(ch, ctx.cfg.messaging.style_examples)
    subject = ch.subject
    learn_style(ctx.services.get("memory"), ch.app, samples)
    try:
        draft = await drafter.draft(ch, thread, intent, samples=samples, subject=subject)
    except ValueError:
        raise SkillError("What would you like to say, sir?") from None
    ctx.state[DRAFT_KEY] = draft
    ctx.state.pop("last_inserted", None)
    ctx.state.pop("alert_offer", None)
    label = APP_LABEL.get(ch.app, ch.app)
    subj = f" Subject: {draft.subject}." if ch.app == "gmail" else ""
    return f"Here's the draft for {ch.name}, sir.{subj} \"{draft.body}\" Shall I put it in {label}?"


def _current(ctx: ToolContext) -> Draft:
    d = ctx.state.get(DRAFT_KEY)
    if d is None:
        if ctx.state.get("last_inserted"):
            raise SkillError("That draft is already in the compose box. I never send messages, so press Return there when you're ready.")
        raise SkillError("There's no draft in progress, sir.")
    return d


async def _revise_draft(ctx: ToolContext, a: dict[str, Any]) -> str:
    hub, drafter = _svc(ctx, "comms"), _svc(ctx, "drafter")
    d = _current(ctx)
    thread = await hub.thread(d.channel, 15)
    samples = await hub.sent_samples(d.channel, ctx.cfg.messaging.style_examples)
    try:
        await drafter.revise(d, a["instruction"], thread, samples)
    except ValueError as e:
        raise SkillError(str(e)) from None
    return f"Revised, sir. \"{d.body}\" Shall I put it in {APP_LABEL.get(d.channel.app, d.channel.app)}?"


async def _open_chat(ctx: ToolContext, a: dict[str, Any]) -> str:
    if not (a.get("person") or "").strip():
        raise SkillError("Whose chat, sir?")
    app = {"messages": "imessage"}.get(a.get("app") or "", a.get("app") or "whatsapp")
    ch = await _resolve_channel(ctx, {"person": a["person"], "app": app})
    return await _svc(ctx, "inserter").open_chat(ch)


async def _insert_draft(ctx: ToolContext, a: dict[str, Any]) -> str:
    d = _current(ctx)
    res = await _svc(ctx, "inserter").insert(d)
    ctx.state.pop(DRAFT_KEY, None)
    ctx.state["last_inserted"] = True
    return res.message


async def _discard_draft(ctx: ToolContext, a: dict[str, Any]) -> str:
    ctx.state.pop(DRAFT_KEY, None)
    return "Draft discarded, sir."


async def _decline_offer(ctx: ToolContext, a: dict[str, Any]) -> str:
    offer = ctx.state.pop("alert_offer", None)
    if offer and (policy := ctx.services.get("offers")):
        policy.snooze(offer.get("thread_id") or offer.get("item_id", ""))
    return "Very good, sir. I'll leave that one for a while."


# ---- VIPs -----------------------------------------------------------------------------------------------


async def _vip_add(ctx: ToolContext, a: dict[str, Any]) -> str:
    vip = _svc(ctx, "vip")
    return f"{a['name']} is now a VIP, sir." if vip.add(a["name"]) else f"{a['name']} is already on the list, sir."


async def _vip_remove(ctx: ToolContext, a: dict[str, Any]) -> str:
    vip = _svc(ctx, "vip")
    return f"{a['name']} is no longer a VIP, sir." if vip.remove(a["name"]) else f"I don't have {a['name']} on the list, sir."


async def _vip_list(ctx: ToolContext, a: dict[str, Any]) -> str:
    names = _svc(ctx, "vip").names()
    return "Your VIPs are " + ", ".join(names) + ", sir." if names else "You haven't named any VIPs yet, sir."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "read_messages",
        "Read the user's unread messages (WhatsApp, iMessage, email). Without 'person' you get a short summary per "
        "conversation (use it for 'any new messages' and counts). With 'person' you get the FULL TEXT of what they sent: "
        "always pass 'person' when the user asks what someone said or to read someone's messages. Never include phone numbers.",
        obj({"source": E("Which app", ["all", "whatsapp", "imessage", "gmail"]), "person": S("Only messages from this person"),
             "limit": I("How many messages to read, default 10")}),
        _read_messages, category="messaging", untrusted_output=True, timeout_s=30))
    reg.register(Tool(
        "read_conversation",
        "Read the recent back-and-forth with one person (what they said AND what the user replied), even if already read. "
        "Use for 'what did Naan say', 'show my chat with Mom', 'what was our last conversation'. Read the result aloud.",
        obj({"person": S("Contact or group name as spoken"), "app": E("Force an app", ["whatsapp", "imessage", "gmail"]),
             "limit": I("How many recent messages, default 12")}, required=["person"]),
        _read_conversation, category="messaging", untrusted_output=True, timeout_s=30))
    reg.register(Tool(
        "search_messages", "Search the user's recent messages and emails (last two weeks) for a word, name or topic.",
        obj({"query": S("Text to look for")}, required=["query"]),
        _search_messages, category="messaging", untrusted_output=True, timeout_s=30))
    reg.register(Tool(
        "check_linkedin", "Check LinkedIn for unread messages (best effort; drives the browser).",
        obj(), _check_linkedin, category="messaging", untrusted_output=True, timeout_s=60))
    reg.register(Tool(
        "draft_message",
        "Draft a message or email in the user's voice. It is NEVER sent. Use for 'message X', 'reply to X', 'tell X that ...', "
        "'email X'. 'message' is what the user wants to say (their intent or exact words). If the user is answering an offer to draft "
        "a reply and names no one, omit 'person'. Afterwards read the draft aloud and ask whether to put it in the app.",
        obj({
            "person": S("Contact name as spoken, e.g. 'Rahul' or 'my brother'. Omit to reply to the message just announced."),
            "message": S("What to say (intent or exact words)"),
            "app": E("Force a channel", APPS),
            "reply": {"type": "boolean", "description": "True when replying to a message they just received"},
        }, required=["message"]),
        _draft_message, category="messaging", timeout_s=40))
    reg.register(Tool(
        "open_chat",
        "Open the conversation with a person in WhatsApp (default) or iMessage so the user can see it. Use for 'open my chat with X', "
        "'open X on WhatsApp', 'open the WhatsApp chat with X'. Does not write or send anything.",
        obj({"person": S("Contact or group name as spoken"), "app": E("Which app, default whatsapp", ["whatsapp", "imessage"])},
            required=["person"]),
        _open_chat, category="messaging", timeout_s=40))
    reg.register(Tool(
        "revise_draft", "Change the current draft ('shorter', 'more formal', 'add that I'll call tonight').",
        obj({"instruction": S("How to change it")}, required=["instruction"]), _revise_draft, category="messaging", timeout_s=40))
    reg.register(Tool(
        "insert_draft",
        "Put the current draft into the app's compose box (or Gmail drafts) for the user to review. Call only after the user agrees. "
        "This does NOT send; the user presses send themselves.",
        obj(), _insert_draft, category="messaging", timeout_s=60))
    reg.register(Tool("discard_draft", "Throw away the current draft.", obj(), _discard_draft, category="messaging"))
    reg.register(Tool(
        "decline_offer", "The user said 'not now' to an offer to draft a reply; stop offering for that conversation for a while.",
        obj(), _decline_offer, category="messaging"))
    reg.register(Tool("vip_add", "Add a person to the VIP list (their messages are announced immediately).",
                      obj({"name": S("Person's name")}, required=["name"]), _vip_add, category="messaging"))
    reg.register(Tool("vip_remove", "Remove a person from the VIP list.",
                      obj({"name": S("Person's name")}, required=["name"]), _vip_remove, category="messaging"))
    reg.register(Tool("vip_list", "List the VIPs.", obj(), _vip_list, category="messaging"))
    _ = I
