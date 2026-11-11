"""Rich cards: structured data that rides along with a reply and is drawn by the Shell.

A card is plain data, never markup. The Shell owns the look, so nothing a model, an email or a web page says can
put HTML or a script in the window. ``clean`` is the one gate every card passes through, which keeps sizes bounded
and the three action kinds (say, url, copy) the only things a button can do.

Schema (every key optional except ``kind``)::

    {"kind": "tile|facts|glance|list|media|code", "tone": "normal|confirm|error|success",
     "icon": "envelope", "kicker": "Email · 4:32 pm", "title": "...", "subtitle": "...",
     "big": "26°", "big_note": "Light rain", "body": "...", "code": "...", "lang": "python",
     "rows": [{"label": "Nifty", "value": "24,310", "tone": "up|down|warn|"}],
     "items": [{"title": "...", "subtitle": "...", "trailing": "4:32", "unread": true, "initial": "N"}],
     "chips": [{"text": "17 Nov", "tone": "warn|"}], "progress": 0.41, "footnote": "...",
     "actions": [{"label": "Open in Gmail", "kind": "say|url|copy", "value": "...", "primary": true}]}
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

KINDS = {"tile", "facts", "glance", "list", "media", "code"}
TONES = {"normal", "confirm", "error", "success"}
ROW_TONES = {"", "up", "down", "warn"}
ACTION_KINDS = {"say", "url", "copy"}
MAX_ROWS, MAX_ITEMS, MAX_CHIPS, MAX_ACTIONS = 12, 8, 6, 3

_SF = re.compile(r"^[a-z0-9][a-z0-9.]{0,40}$")


def _s(v: Any, n: int) -> str:
    s = " ".join(str(v).split()) if v is not None else ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _long(v: Any, n: int) -> str:
    s = str(v).replace("\r", "").strip() if v is not None else ""
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _safe_url(u: Any) -> str:
    s = str(u or "").strip()
    p = urlparse(s)
    return s if p.scheme in ("http", "https") and p.netloc else ""


def clean(card: Any) -> dict[str, Any] | None:
    """A bounded, well-formed copy of ``card``, or ``None`` when there is nothing worth showing."""
    if not isinstance(card, dict) or card.get("kind") not in KINDS:
        return None
    out: dict[str, Any] = {"kind": card["kind"], "tone": card.get("tone") if card.get("tone") in TONES else "normal"}
    for key, n in (("kicker", 80), ("title", 140), ("subtitle", 140), ("big", 24), ("big_note", 60), ("footnote", 160), ("lang", 20)):
        if card.get(key):
            out[key] = _s(card[key], n)
    if card.get("body"):
        out["body"] = _long(card["body"], 600)
    if card.get("code"):
        out["code"] = _long(card["code"], 1500)
    icon = str(card.get("icon") or "")
    if _SF.match(icon):
        out["icon"] = icon
    rows = [{"label": _s(r.get("label"), 40), "value": _s(r.get("value"), 80),
             "tone": r.get("tone") if r.get("tone") in ROW_TONES else ""}
            for r in (card.get("rows") or []) if isinstance(r, dict) and (r.get("label") or r.get("value"))]
    if rows:
        out["rows"] = rows[:MAX_ROWS]
    items = [{"title": _s(i.get("title"), 80), "subtitle": _s(i.get("subtitle"), 90), "trailing": _s(i.get("trailing"), 20),
              "unread": bool(i.get("unread")), "initial": _s(i.get("initial"), 2)}
             for i in (card.get("items") or []) if isinstance(i, dict) and i.get("title")]
    if items:
        out["items"] = items[:MAX_ITEMS]
    chips = [{"text": _s(c.get("text") if isinstance(c, dict) else c, 32),
              "tone": "warn" if isinstance(c, dict) and c.get("tone") == "warn" else ""}
             for c in (card.get("chips") or []) if (c.get("text") if isinstance(c, dict) else c)]
    if chips:
        out["chips"] = chips[:MAX_CHIPS]
    p = card.get("progress")
    if isinstance(p, (int, float)) and not isinstance(p, bool):
        out["progress"] = max(0.0, min(1.0, float(p)))
    actions = []
    for a in card.get("actions") or []:
        if not isinstance(a, dict) or a.get("kind") not in ACTION_KINDS or not a.get("label"):
            continue
        value = _safe_url(a.get("value")) if a["kind"] == "url" else _s(a.get("value"), 400)
        if value:
            actions.append({"label": _s(a["label"], 28), "kind": a["kind"], "value": value, "primary": bool(a.get("primary"))})
    if actions:
        for extra in actions[1:]:
            extra["primary"] = False  # one primary at most
        out["actions"] = actions[:MAX_ACTIONS]
    shown = ("title", "big", "rows", "items", "body", "code", "chips")
    return out if any(out.get(k) for k in shown) else None


def post(ctx: Any, card: dict[str, Any]) -> None:
    """Called by a tool: attach ``card`` to the reply this tool call belongs to."""
    c = clean(card)
    if c is not None:
        ctx.state["card"] = c


def say(label: str, text: str, primary: bool = False) -> dict[str, Any]:
    return {"label": label, "kind": "say", "value": text, "primary": primary}


def link(label: str, url: str, primary: bool = False) -> dict[str, Any]:
    return {"label": label, "kind": "url", "value": url, "primary": primary}


def copy(label: str, text: str) -> dict[str, Any]:
    return {"label": label, "kind": "copy", "value": text}


def clock(dt: datetime | None) -> str:
    if dt is None:
        return ""
    d = dt.astimezone() if dt.tzinfo else dt
    return d.strftime("%-I:%M %p").lower()


def signed(pct: float) -> tuple[str, str]:
    return (f"{'+' if pct >= 0 else '−'}{abs(pct):.1f}%", "up" if pct >= 0 else "down")


# ---- builders from structured data -----------------------------------------------------------------------------------


def weather_card(w: Any) -> dict[str, Any]:
    rows = []
    if w.rain_next_hours:
        for off, p in w.rain_next_hours[:4]:
            rows.append({"label": "Now" if off == 0 else f"+{off} h", "value": f"{p}% rain", "tone": "warn" if p >= 50 else ""})
    chips = []
    if w.wind_kmh:
        chips.append(f"Wind {w.wind_kmh:.0f} km/h")
    if w.sunset:
        chips.append(f"Sunset {w.sunset}")
    if w.aqi is not None:
        chips.append(f"AQI {w.aqi}")
    if w.uv_max is not None:
        chips.append(f"UV {w.uv_max:.0f}")
    return {"kind": "glance", "icon": "cloud.sun", "kicker": w.city, "big": f"{w.temp_c:.0f}°",
            "big_note": f"{w.condition} · feels {w.feels_c:.0f}°", "subtitle": f"High {w.high_c:.0f}°, low {w.low_c:.0f}°",
            "rows": rows, "chips": chips}


def markets_card(quotes: list[Any]) -> dict[str, Any]:
    rows = []
    for q in quotes:
        price = f"{q.price:,.0f}" if q.price >= 100 else f"{q.price:,.2f}"
        text, tone = signed(q.change_pct)
        rows.append({"label": q.name or q.symbol, "value": f"{price}  {text}", "tone": tone})
    return {"kind": "facts", "icon": "chart.line.uptrend.xyaxis", "kicker": "Markets", "rows": rows,
            "footnote": "Delayed quotes"}


def calendar_card(events: list[Any], when: str = "Today") -> dict[str, Any]:
    items = []
    for e in events:
        trailing = "All day" if getattr(e, "all_day", False) else clock(e.start)
        items.append({"title": e.title, "subtitle": getattr(e, "location", "") or "", "trailing": trailing})
    first = next((e.link for e in events if getattr(e, "link", "")), "")
    return {"kind": "list", "icon": "calendar", "kicker": when, "items": items, "title": "" if items else "Nothing on",
            "footnote": "" if items else "Clear day.", "actions": [link("Open event", first, True)] if first else []}


def news_card(items: list[Any], topic: str) -> dict[str, Any]:
    rows = [{"title": h.title, "subtitle": h.source, "trailing": clock(h.published)} for h in items[:6]]
    first = next((h.link for h in items if getattr(h, "link", "")), "")
    acts = [link("Open top story", first, True)] if first else []
    return {"kind": "list", "icon": "newspaper", "kicker": f"News · {topic}", "items": rows, "actions": acts}


def email_card(item: Any, text: str) -> dict[str, Any]:
    chips: list[Any] = []
    for m in re.finditer(r"\b(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?:\s+\d{4})?)", text):
        chips.append(m.group(1))
        if len(chips) >= 3:
            break
    ctc = re.search(r"(\d+(?:\.\d+)?\s*(?:LPA|lpa|lakhs?))", text)
    if ctc:
        chips.append(ctc.group(1))
    url = f"https://mail.google.com/mail/u/0/#all/{item.thread_id or item.id}"
    return {"kind": "tile", "icon": "envelope", "kicker": f"Email · {item.sender} · {clock(item.timestamp)}".strip(" ·"),
            "title": item.title, "body": text[:600], "chips": chips,
            "actions": [link("Open in Gmail", url, True), say("Read it all", f"read the whole email from {item.sender} aloud"),
                        say("Draft reply", f"draft a reply to the email from {item.sender}")]}


def messages_card(items: list[Any]) -> dict[str, Any]:
    labels = {"whatsapp": "WhatsApp", "imessage": "iMessage", "gmail": "Email"}
    rows = [{"title": f"{i.sender} · {labels.get(i.source, i.source)}", "subtitle": (i.title + ". " if i.source == "gmail" and i.title else "") + (i.body or ""),
             "trailing": clock(i.timestamp), "unread": True, "initial": (i.sender or "?")[:1].upper()} for i in items[:6]]
    first = items[0].sender if items else ""
    acts = [say(f"Reply to {first}", f"reply to {first}", True)] if first else []
    return {"kind": "list", "icon": "bubble.left.and.bubble.right", "kicker": f"{len(items)} unread", "items": rows, "actions": acts}


def draft_card(channel_name: str, app_label: str, body: str, subject: str = "") -> dict[str, Any]:
    return {"kind": "tile", "icon": "square.and.pencil", "kicker": f"Draft for {channel_name} · {app_label}", "title": subject,
            "body": body, "footnote": "Never sent for you: you press Return.",
            "actions": [say(f"Put in {app_label}", "yes", True), say("Change", "change the draft"), say("Not now", "not now")]}


def files_card(paths: list[str], home: str) -> dict[str, Any]:
    from pathlib import Path

    items = []
    for p in paths[:6]:
        pp = Path(p)
        items.append({"title": pp.name, "subtitle": str(pp.parent).replace(home, "~"), "initial": (pp.suffix[1:4] or "F").upper()[:2]})
    acts = [say("Open the first", f"open {Path(paths[0]).name}", True)] if paths else []
    return {"kind": "list", "icon": "doc.text.magnifyingglass", "kicker": f"{len(paths)} found", "items": items, "actions": acts}


def done_card(title: str, subtitle: str = "", undo: bool = False) -> dict[str, Any]:
    return {"kind": "tile", "tone": "success", "icon": "checkmark.circle", "title": title, "subtitle": subtitle,
            "actions": [say("Undo", "undo that")] if undo else []}


def confirm_card(question: str, detail: str = "") -> dict[str, Any]:
    return {"kind": "tile", "tone": "confirm", "icon": "exclamationmark.triangle", "kicker": "Needs your OK", "title": question,
            "body": detail, "actions": [say("Yes, go ahead", "yes", True), say("Cancel", "no")]}


def error_card(what: str, fix: str = "") -> dict[str, Any]:
    rows = [{"label": "What", "value": what}] + ([{"label": "Fix", "value": fix}] if fix else [])
    return {"kind": "tile", "tone": "error", "icon": "exclamationmark.octagon", "kicker": "Problem", "rows": rows}


# ---- builders that read a tool's text result -------------------------------------------------------------------------


def auto_card(name: str, args: dict[str, Any], result: str, ctx: Any) -> dict[str, Any] | None:
    """A card for tools whose useful output is one plain sentence. ``None`` means this tool gets no card."""
    r = (result or "").strip()
    if not r or r.startswith(("Error:", "Blocked", "Cancelled")):
        return None
    if name == "battery_status":
        m = re.search(r"(\d+) percent, ([a-z ]+?)(?:,|\.)", r)
        left = re.search(r"about (.+?) left", r)
        if m:
            pct = int(m.group(1))
            return {"kind": "glance", "icon": "battery.50", "kicker": "Battery", "big": f"{pct}%", "big_note": m.group(2).strip().capitalize(),
                    "subtitle": f"About {left.group(1)} left" if left else "", "progress": pct / 100}
    if name == "system_status":
        rows = []
        for key, pat in (("CPU", r"CPU at (\d+) percent"), ("Memory", r"memory (\d+) percent"), ("Disk free", r"(\d+) gigabytes free"),
                         ("Uptime", r"up (.+?)(?:,|\.$)"), ("Battery", r"battery (\d+) percent")):
            m = re.search(pat, r)
            if m:
                unit = {"CPU": "%", "Memory": "% used", "Disk free": " GB", "Battery": "%"}.get(key, "")
                rows.append({"label": key, "value": m.group(1) + unit})
        return {"kind": "facts", "icon": "gauge", "kicker": "This Mac", "rows": rows} if rows else None
    if name == "now_playing" and " by " in r and r != "Nothing is playing.":
        track, artist = r.rsplit(" by ", 1)
        return {"kind": "media", "icon": "music.note", "title": track, "subtitle": artist,
                "actions": [say("Pause", "pause the music", True), say("Next", "next song"), say("Previous", "previous song")]}
    if name == "calculate":
        expr = str(args.get("expression") or "")
        return {"kind": "glance", "icon": "function", "kicker": _s(expr, 60), "big": _s(r.split("=")[-1].strip(), 24),
                "body": r if len(r) > 24 else "", "actions": [copy("Copy", r)]}
    if name in ("timer_set", "alarm_set", "reminder_create"):
        return done_card(r.rstrip("."), "", undo=False) | {"icon": "alarm" if name != "reminder_create" else "checklist"}
    if name in ("move_file", "move_to_trash", "copy_file", "make_folder"):
        return done_card(r.rstrip("."), "", undo=name in ("move_file", "move_to_trash"))
    if name == "undo_last_change":
        return done_card(r.rstrip("."), "")
    if name == "run_command":
        ok = "exit code" not in r.lower() or "exit code 0" in r.lower()
        return {"kind": "code", "tone": "normal" if ok else "error", "icon": "terminal", "kicker": _s(str(args.get("command") or ""), 80),
                "code": r, "actions": [copy("Copy output", r)]}
    if name == "web_search":
        return None
    return None
