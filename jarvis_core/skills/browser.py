"""Browser control (tabs) and web access (search, fetch)."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus

from jarvis_core import cards
from jarvis_core.errors import SkillError
from jarvis_core.skills.apps import check_url, resolve_app, running_apps
from jarvis_core.skills.base import S, Tool, ToolContext, ToolRegistry, obj

CHROMIUM = ("Google Chrome", "Brave Browser", "Microsoft Edge", "Arc", "Chromium", "Vivaldi", "Opera", "Dia")
BROWSERS = ("Safari", *CHROMIUM, "Firefox")

_LIST_CHROMIUM = """on run argv
set out to ""
tell application "APPNAME"
repeat with w in windows
set i to 0
repeat with t in tabs of w
set i to i + 1
set out to out & i & " | " & (title of t) & " | " & (URL of t) & linefeed
end repeat
end repeat
end tell
return out
end run"""

_LIST_SAFARI = """on run argv
set out to ""
tell application "Safari"
repeat with w in windows
set i to 0
repeat with t in tabs of w
set i to i + 1
set out to out & i & " | " & (name of t) & " | " & (URL of t) & linefeed
end repeat
end repeat
end tell
return out
end run"""

_CURRENT_CHROMIUM = """on run argv
tell application "APPNAME"
set t to active tab of front window
return (title of t) & " | " & (URL of t)
end tell
end run"""

_CURRENT_SAFARI = """tell application "Safari"
set t to current tab of front window
return (name of t) & " | " & (URL of t)
end tell"""

_FOCUS_CHROMIUM = """on run argv
set needle to item 1 of argv
tell application "APPNAME"
repeat with w in windows
set i to 0
repeat with t in tabs of w
set i to i + 1
if (title of t) contains needle or (URL of t) contains needle then
set active tab index of w to i
set index of w to 1
activate
return title of t
end if
end repeat
end repeat
end tell
return ""
end run"""

_FOCUS_SAFARI = """on run argv
set needle to item 1 of argv
tell application "Safari"
repeat with w in windows
repeat with t in tabs of w
if (name of t) contains needle or (URL of t) contains needle then
set current tab of w to t
set index of w to 1
activate
return name of t
end if
end repeat
end repeat
end tell
return ""
end run"""


_SAFE_APP = __import__("re").compile(r"^[A-Za-z0-9 .+\-]{1,40}$")


def chromium_script(template: str, app: str) -> str:
    """AppleScript can only resolve a browser's terminology (tabs, URL, active tab) when the app is named literally in the
    script, so the name is put in here, after checking it is a plain app name (it is never taken from spoken text unchecked)."""
    if not _SAFE_APP.match(app):
        raise SkillError("I can't script that browser, sir.")
    return template.replace("APPNAME", app)


async def active_browser(ctx: ToolContext, preferred: str | None = None) -> str:
    if preferred:
        return resolve_app(ctx, preferred)
    running = await running_apps(ctx)
    for b in BROWSERS:
        if b in running:
            return b
    raise SkillError("No scriptable browser is open, sir.")


def _split(out: str) -> list[tuple[str, str, str]]:
    rows = []
    for ln in out.splitlines():
        parts = [p.strip() for p in ln.split(" | ", 2)]
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


async def _tabs(ctx: ToolContext, a: dict[str, Any]) -> str:
    b = await active_browser(ctx, a.get("browser"))
    out = await (ctx.osa(_LIST_SAFARI) if b == "Safari" else ctx.osa(chromium_script(_LIST_CHROMIUM, b)))
    rows = _split(out)
    if not rows:
        return f"No tabs open in {b}."
    return f"{b}, {len(rows)} tabs:\n" + "\n".join(f"{t}  ({u[:80]})" for _, t, u in rows[:30])


async def _current(ctx: ToolContext, a: dict[str, Any]) -> str:
    b = await active_browser(ctx, a.get("browser"))
    out = await (ctx.osa(_CURRENT_SAFARI) if b == "Safari" else ctx.osa(chromium_script(_CURRENT_CHROMIUM, b)))
    title, _, url = out.partition(" | ")
    return f"{b}: {title} ({url})"


async def _focus_tab(ctx: ToolContext, a: dict[str, Any]) -> str:
    b = await active_browser(ctx, a.get("browser"))
    out = await (ctx.osa(_FOCUS_SAFARI, a["match"]) if b == "Safari" else ctx.osa(chromium_script(_FOCUS_CHROMIUM, b), a["match"]))
    if not out:
        raise SkillError(f"I couldn't find a tab matching {a['match']}, sir.")
    return f"Switched to {out}."


async def _search_in_browser(ctx: ToolContext, a: dict[str, Any]) -> str:
    url = "https://www.google.com/search?q=" + quote_plus(a["query"])
    argv = ["open"]
    if a.get("browser"):
        argv += ["-a", resolve_app(ctx, a["browser"])]
    res = await ctx.run([*argv, url])
    if not res.ok:
        raise SkillError("I couldn't open the browser, sir.")
    return f"Searching for {a['query']}."


# -- web (reads the page content for the model, not the screen) -------------------


async def _web_search(ctx: ToolContext, a: dict[str, Any]) -> str:
    from jarvis_core.adapters.services import web_search

    q = a["query"]
    text = await asyncio.get_running_loop().run_in_executor(None, web_search, q, 5)
    rows = []
    for ln in text.splitlines():
        m = re.match(r"(.*?): (.*) \((https?://[^)\s]+)\)\s*$", ln)
        if m:
            rows.append((m.group(1), m.group(3)))
    if rows:
        from urllib.parse import urlparse

        cards.post(ctx, {"kind": "list", "icon": "magnifyingglass", "kicker": f"Sources · {q}",
                         "items": [{"title": t, "subtitle": urlparse(u).netloc.removeprefix("www."), "initial": str(i + 1)}
                                   for i, (t, u) in enumerate(rows)],
                         "actions": [cards.link("Open top result", rows[0][1], True)]})
    return text


async def _web_fetch(ctx: ToolContext, a: dict[str, Any]) -> str:
    from jarvis_core.adapters.services import fetch_url

    url = check_url(a["url"])
    return await asyncio.get_running_loop().run_in_executor(None, fetch_url, url, 6000)


def register(reg: ToolRegistry) -> None:
    browser = {"browser": S("Optional browser name; default is the one that's open")}
    reg.register(Tool("browser_tabs", "List open browser tabs.", obj(browser), _tabs, category="browser", untrusted_output=True))
    reg.register(Tool("browser_current_page", "Title and URL of the page in front.", obj(browser), _current, category="browser", untrusted_output=True))
    reg.register(Tool(
        "browser_focus_tab", "Switch to the tab whose title or URL contains the text.",
        obj({"match": S("Text in the tab title or URL"), **browser}, ["match"]), _focus_tab, category="browser"))
    reg.register(Tool(
        "search_in_browser", "Show a Google search in the browser window (visible to the user).",
        obj({"query": S("Search terms"), **browser}, ["query"]), _search_in_browser, category="browser"))
    reg.register(Tool(
        "web_search", "Search the web and return result snippets, for answering questions.",
        obj({"query": S("Search query")}, ["query"]), _web_search, category="web", untrusted_output=True, timeout_s=25))
    reg.register(Tool(
        "web_fetch", "Fetch and read a web page as text.",
        obj({"url": S("Page URL")}, ["url"]), _web_fetch, category="web", untrusted_output=True, timeout_s=25))
