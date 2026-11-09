"""Applications: open, quit, focus, URLs and paths."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from rapidfuzz import fuzz, process

from jarvis_core.errors import SkillError
from jarvis_core.policy.protected import guard
from jarvis_core.skills.base import B, Decision, Risk, S, Tool, ToolContext, ToolRegistry, obj

ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code", "code": "Visual Studio Code", "word": "Microsoft Word",
    "excel": "Microsoft Excel", "powerpoint": "Microsoft PowerPoint", "teams": "Microsoft Teams",
    "outlook": "Microsoft Outlook", "settings": "System Settings", "system preferences": "System Settings",
    "preferences": "System Settings", "mail": "Mail", "messages": "Messages", "imessage": "Messages",
    "whatsapp": "WhatsApp", "spotify": "Spotify", "music": "Music", "notes": "Notes",
    "reminders": "Reminders", "calendar": "Calendar", "finder": "Finder", "terminal": "Terminal",
    "iterm": "iTerm", "safari": "Safari", "slack": "Slack", "zoom": "zoom.us", "facetime": "FaceTime",
    "activity monitor": "Activity Monitor", "photos": "Photos", "maps": "Maps", "cursor": "Cursor",
}

_INDEX_TTL = 120.0


def installed_apps(ctx: ToolContext) -> dict[str, str]:
    """name -> path for every .app in the standard folders (cached briefly)."""
    cached = ctx.state.get("apps_index")
    if cached and time.monotonic() - cached[0] < _INDEX_TTL:
        return cached[1]
    found: dict[str, str] = {}
    for d in ctx.app_dirs:
        try:
            for p in d.iterdir():
                if p.suffix == ".app":
                    found.setdefault(p.stem, str(p))
                elif d.name == "Applications" and d.parent == Path("/") and p.is_dir() and not p.name.startswith("."):
                    for q in p.iterdir():  # one level of vendor folders: /Applications/Adobe .../*.app
                        if q.suffix == ".app":
                            found.setdefault(q.stem, str(q))
        except OSError:
            continue
    ctx.state["apps_index"] = (time.monotonic(), found)
    return found


# Words that point at "whatever is in front" or at a part of a window, never at an installed app.
FRONT_REFS = frozenset({"this", "that", "it", "this app", "that app", "current", "current app", "front", "frontmost", "front app",
                        "active", "active app", "the window", "this window", "window", "current window", "this one", "that one"})
NOT_AN_APP = FRONT_REFS | {"tab", "this tab", "the tab", "current tab", "apps", "all", "all apps", "everything", "all windows",
                           "windows", "that tab", "downloads folder", "folder", "desktop", "my desktop"}


async def front_app(ctx: ToolContext) -> str:
    """The app the user is looking at (not Jarvis itself)."""
    out = await ctx.osa('tell application "System Events" to get name of first application process whose frontmost is true')
    if out in ("Jarvis", "JarvisShell"):
        raise SkillError("Which app do you mean, sir?")
    return out


async def target_app(ctx: ToolContext, name: str) -> str:
    """An app name, or "this"/"it"/"the window"/empty meaning the front app."""
    if not name.strip() or name.strip().lower() in FRONT_REFS:
        return await front_app(ctx)
    return resolve_app(ctx, name)


def resolve_app(ctx: ToolContext, name: str) -> str:
    """Map a spoken app name to an installed app name."""
    name = name.strip().strip(".")
    if name.lower() in NOT_AN_APP:
        raise SkillError("Which app do you mean, sir?")
    apps = installed_apps(ctx)
    key = name.lower()
    if key in ALIASES and ALIASES[key] in apps:
        return ALIASES[key]
    for a in apps:
        if a.lower() == key:
            return a
    if not apps:
        return ALIASES.get(key, name)
    best = process.extractOne(name, list(apps), scorer=fuzz.WRatio, processor=str.lower)
    # WRatio alone also scores partial overlaps ("this tab" ~ "About This Mac"), so the whole name must be close too.
    if best and best[1] >= 82 and fuzz.ratio(name.lower(), best[0].lower()) >= 60:
        return best[0]
    near = [m[0] for m in process.extract(name, list(apps), scorer=fuzz.WRatio, processor=str.lower, limit=3)]
    raise SkillError(f"I couldn't find an app called {name}, sir. Did you mean {', '.join(near)}?")


async def running_apps(ctx: ToolContext) -> list[str]:
    out = await ctx.osa(
        'tell application "System Events" to get name of every process whose background only is false'
    )
    return [s.strip() for s in out.split(",") if s.strip()]


_FILLER_TAIL = re.compile(r"\s+(?:on|in|at|from|for)\s+.*$|\s+(?:app|application|website|site|mode|routine)$", re.I)


async def _open_app(ctx: ToolContext, a: dict[str, Any]) -> str:
    try:
        app = resolve_app(ctx, a["name"])
    except SkillError as err:
        # Not an installed app: it may be a website ("youtube") or a routine ("work mode"). Try those instead of failing.
        said = a["name"].strip().lower()
        core = _FILLER_TAIL.sub("", said).strip()
        if said.endswith(("mode", "routine")) or core in {n.lower() for n in ctx.cfg.routines}:
            from jarvis_core.skills.routines import find_routine, run_routine_by_name  # noqa: PLC0415

            try:
                return await run_routine_by_name(ctx, find_routine(ctx, core))
            except SkillError:
                pass
        if core in SITES or said in SITES:
            return await _open_site(ctx, {"name": core if core in SITES else said})
        raise err
    res = await ctx.run(["open", "-a", app])
    if not res.ok:
        raise SkillError(f"I couldn't open {app}, sir.")
    return f"Opened {app}."


async def _focus_app(ctx: ToolContext, a: dict[str, Any]) -> str:
    app = resolve_app(ctx, a["name"])
    res = await ctx.run(["open", "-a", app])
    if not res.ok:
        raise SkillError(f"I couldn't bring up {app}, sir.")
    return f"{app} is in front."


async def _quit_app(ctx: ToolContext, a: dict[str, Any]) -> str:
    guard(a["name"], ctx.cfg)
    app = resolve_app(ctx, a["name"])
    guard(app, ctx.cfg)
    await ctx.osa("on run argv\ntell application (item 1 of argv) to quit\nend run", app)
    return f"Asked {app} to quit."


async def _force_quit(ctx: ToolContext, a: dict[str, Any]) -> str:
    guard(a["name"], ctx.cfg)
    app = resolve_app(ctx, a["name"])
    guard(app, ctx.cfg)
    res = await ctx.run(["pkill", "-x", app])
    if not res.ok:
        raise SkillError(f"{app} doesn't seem to be running, sir.")
    return f"Force quit {app}."


async def _list_running(ctx: ToolContext, a: dict[str, Any]) -> str:
    apps = await running_apps(ctx)
    return ", ".join(apps) if apps else "No visible apps are running."


SAFE_SCHEMES = {"http", "https"}


def check_url(url: str) -> str:
    url = url.strip()
    if "://" not in url and re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(:\d+)?([/?#].*)?$", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    if p.scheme not in SAFE_SCHEMES or not p.netloc:
        raise SkillError("I can only open web links, sir.")
    return url


async def _open_url(ctx: ToolContext, a: dict[str, Any]) -> str:
    url = check_url(a["url"])
    argv = ["open"]
    if a.get("browser"):
        argv += ["-a", resolve_app(ctx, a["browser"])]
    res = await ctx.run([*argv, url])
    if not res.ok:
        raise SkillError("I couldn't open that link, sir.")
    return f"Opened {urlparse(url).netloc}."


SITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "github": "https://github.com",
    "google": "https://www.google.com", "twitter": "https://x.com", "x": "https://x.com", "reddit": "https://www.reddit.com",
    "netflix": "https://www.netflix.com", "linkedin": "https://www.linkedin.com", "facebook": "https://www.facebook.com",
    "instagram": "https://www.instagram.com", "amazon": "https://www.amazon.in", "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai", "stackoverflow": "https://stackoverflow.com", "stack overflow": "https://stackoverflow.com",
    "wikipedia": "https://www.wikipedia.org", "google drive": "https://drive.google.com", "drive": "https://drive.google.com",
    "google maps": "https://maps.google.com", "google docs": "https://docs.google.com", "google sheets": "https://sheets.google.com",
    "overleaf": "https://www.overleaf.com", "leetcode": "https://leetcode.com", "google calendar": "https://calendar.google.com",
    "hacker news": "https://news.ycombinator.com", "twitch": "https://www.twitch.tv", "spotify web": "https://open.spotify.com",
}


async def _open_site(ctx: ToolContext, a: dict[str, Any]) -> str:
    name = a["name"].strip().lower().removesuffix(" website").removesuffix(" site").removesuffix(".com")
    url = SITES.get(name)
    if url is None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise SkillError(f"I don't know a website called {a['name']}, sir.")
        url = f"https://www.{name}.com"
    res = await ctx.run(["open", url])
    if not res.ok:
        raise SkillError("I couldn't open that site, sir.")
    return f"Opened {urlparse(url).netloc.removeprefix('www.')}."


async def _open_path(ctx: ToolContext, a: dict[str, Any]) -> str:
    from jarvis_core.skills.files import resolve_path

    p = resolve_path(ctx, a["path"])
    if not p.exists():
        raise SkillError("That path doesn't exist, sir.")
    argv = ["open"]
    if a.get("with_app"):
        argv += ["-a", resolve_app(ctx, a["with_app"])]
    res = await ctx.run([*argv, str(p)])
    if not res.ok:
        raise SkillError("I couldn't open that, sir.")
    return f"Opened {p.name}."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "open_site", "Open a well-known website by name (YouTube, Gmail, GitHub, Reddit, LinkedIn, ...) in the browser. "
        "Use for 'open youtube' or 'go to gmail'; open_app is only for installed applications.",
        obj({"name": S("Site name, e.g. 'youtube' or 'github'")}, ["name"]), _open_site, category="apps"))
    name = {"name": S("App name as the user said it, e.g. 'chrome' or 'spotify'")}
    reg.register(Tool("open_app", "Open or launch an application. Call once per app when asked to open several.", obj(name, ["name"]), _open_app, category="apps"))
    reg.register(Tool("focus_app", "Bring an application to the front.", obj(name, ["name"]), _focus_app, category="apps"))
    reg.register(Tool("quit_app", "Close or quit an APPLICATION (Calculator, Safari, Spotify, ...). Use this for 'close X' / 'quit X' when X is an app, "
        "even a browser. Call once per app when asked to close several.", obj(name, ["name"]), _quit_app, category="apps"))
    reg.register(
        Tool(
            "force_quit_app", "Force quit an unresponsive application.", obj(name, ["name"]), _force_quit,
            risk=Risk.CONFIRM, confirm_prompt=lambda a: f"Force quit {a.get('name')}? Unsaved work will be lost.",
            category="apps",
        )
    )
    reg.register(Tool("list_running_apps", "List visible running applications.", obj(), _list_running, category="apps"))
    reg.register(
        Tool(
            "open_url", "Open a web link in the browser.",
            obj({"url": S("http(s) URL or domain"), "browser": S("Optional browser app name")}, ["url"]),
            _open_url, category="apps",
        )
    )
    reg.register(
        Tool(
            "open_path", "Open a file or folder (optionally with a specific app).",
            obj({"path": S("File or folder path, ~ allowed"), "with_app": S("Optional app name")}, ["path"]),
            _open_path, category="apps",
        )
    )
    _ = (B, Decision)
