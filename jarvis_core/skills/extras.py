"""Remaining Mac-control tools: screenshots, Focus, calendar events, deleting reminders, closing tabs/apps."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.apps import running_apps
from jarvis_core.skills.base import E, I, Risk, S, Tool, ToolContext, ToolRegistry, obj
from jarvis_core.skills.browser import active_browser, chromium_script
from jarvis_core.skills.productivity import _MK_DATE, parse_when

# Apps that "quit everything" never touches (the user is talking to Jarvis through them).
KEEP_RUNNING = {"Finder", "Jarvis", "JarvisShell", "SystemUIServer", "Dock"}


# ---- screenshots -------------------------------------------------------------------------------------

_FRONT_WINDOW_RECT = """tell application "System Events"
set p to first process whose frontmost is true
set w to front window of p
set {x, y} to position of w
set {wd, ht} to size of w
return (x as integer) & "," & (y as integer) & "," & (wd as integer) & "," & (ht as integer)
end tell"""


async def _screenshot(ctx: ToolContext, a: dict[str, Any]) -> str:
    folder = ctx.home_dir / "Desktop" / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Jarvis {ctx.clock().strftime('%Y-%m-%d at %H.%M.%S')}.png"
    argv = ["screencapture", "-x"]
    what = "the screen"
    if a.get("target") == "window":
        try:
            rect = (await ctx.osa(_FRONT_WINDOW_RECT)).replace(" ", "")
            if rect.count(",") == 3:
                argv += ["-R", rect]
                what = "that window"
        except SkillError:
            pass  # no Accessibility: fall back to the whole screen
    res = await ctx.run([*argv, str(path)], timeout=15)
    if not res.ok:
        raise SkillError("I couldn't take the screenshot, sir. Screen Recording permission may be missing.")
    return f"Captured {what}. It's saved in Screenshots on your Desktop."


# ---- Focus -------------------------------------------------------------------------------------------


async def _set_focus(ctx: ToolContext, a: dict[str, Any]) -> str:
    """Focus can't be toggled directly; Jarvis runs 'Set Focus <Name>' / 'Turn Off Focus' shortcuts (and offers to make them)."""
    from jarvis_core.adapters import shortcuts as shortcuts_kit

    on = a.get("state", "on") != "off"
    name = (a.get("name") or "Do Not Disturb").strip().title()
    name = {"Dnd": "Do Not Disturb", "D&D": "Do Not Disturb"}.get(name, name)
    await shortcuts_kit.run(ctx, shortcuts_kit.focus_shortcut(name) if on else shortcuts_kit.FOCUS_OFF)
    return f"{name} focus is on." if on else "Focus is off."


async def _make_shortcut(ctx: ToolContext, a: dict[str, Any]) -> str:
    from jarvis_core.adapters import shortcuts as shortcuts_kit

    name = a["name"].strip()
    known = {n.lower(): n for n in [*shortcuts_kit.DEFAULT_SET, *map(shortcuts_kit.focus_shortcut, shortcuts_kit.FOCUS_MODES)]}
    name = known.get(name.lower(), name)
    return await shortcuts_kit.install(ctx, name)


async def _switch(ctx: ToolContext, a: dict[str, Any]) -> str:
    from jarvis_core.adapters import shortcuts as shortcuts_kit

    setting = {k.lower(): k for k in shortcuts_kit.SWITCHES}[a["setting"].lower()]
    on = a["state"] == "on"
    await shortcuts_kit.run(ctx, shortcuts_kit.switch_shortcut(setting, on))
    return f"{setting} is {'on' if on else 'off'}."


# ---- calendar ----------------------------------------------------------------------------------------


async def _event_create(ctx: ToolContext, a: dict[str, Any]) -> str:
    start = parse_when(a["when"], ctx.clock())
    minutes = int(a.get("minutes") or 60)
    if not 5 <= minutes <= 24 * 60:
        raise SkillError("An event should last between five minutes and a day, sir.")
    end = start + timedelta(minutes=minutes)
    script = (
        "on run argv\n"
        + _MK_DATE.format(y=2, mo=3, dd=4, h=5, mi=6)
        + "set s to d\nset e to s + ((item 7 of argv as integer) * minutes)\n"
        + 'tell application "Calendar"\n'
        "if (item 8 of argv) is \"\" then\nset cal to first calendar whose writable is true\nelse\n"
        "set cal to first calendar whose name is (item 8 of argv)\nend if\n"
        "make new event at end of events of cal with properties {summary:(item 1 of argv), start date:s, end date:e}\n"
        "end tell\nend run"
    )
    await ctx.osa(
        script, a["title"].strip(), str(start.year), str(start.month), str(start.day), str(start.hour),
        str(start.minute), str(minutes), (a.get("calendar") or "").strip(), timeout=30,
    )
    return f"Added {a['title'].strip()} on {start.strftime('%A %d %B at %H:%M')}, until {end.strftime('%H:%M')}."


_EVENT_DELETE = """on run argv
set q to item 1 of argv
set now to current date
set later to now + (30 * days)
tell application "Calendar"
repeat with cal in calendars
set hits to (events of cal whose summary contains q and start date >= now and start date <= later)
if (count of hits) > 0 then
set ev to item 1 of hits
set nm to summary of ev
delete ev
return nm
end if
end repeat
end tell
return ""
end run"""


async def _event_delete(ctx: ToolContext, a: dict[str, Any]) -> str:
    out = await ctx.osa(_EVENT_DELETE, a["title"].strip(), timeout=60)
    if not out:
        raise SkillError(f"I found no event matching {a['title']} in the next month, sir.")
    return f"Deleted {out}."


# ---- reminders ---------------------------------------------------------------------------------------


async def _reminder_delete(ctx: ToolContext, a: dict[str, Any]) -> str:
    out = await ctx.osa(
        'on run argv\ntell application "Reminders"\nset hits to (reminders whose name contains (item 1 of argv))\n'
        'if (count of hits) is 0 then return ""\nset r to item 1 of hits\nset nm to name of r\ndelete r\nreturn nm\nend tell\nend run',
        a["title"].strip(), timeout=40,
    )
    if not out:
        raise SkillError(f"I couldn't find a reminder matching {a['title']}, sir.")
    return f"Deleted the reminder {out}."


# ---- browser tabs ------------------------------------------------------------------------------------

_CLOSE_TAB_CHROMIUM = """on run argv
set needle to item 1 of argv
tell application "APPNAME"
if needle is "" then
set t to active tab of front window
set nm to title of t
close t
return nm
end if
repeat with w in windows
repeat with t in tabs of w
if (title of t) contains needle or (URL of t) contains needle then
set nm to title of t
close t
return nm
end if
end repeat
end repeat
end tell
return ""
end run"""

_CLOSE_TAB_SAFARI = """on run argv
set needle to item 1 of argv
tell application "Safari"
if needle is "" then
set t to current tab of front window
set nm to name of t
close t
return nm
end if
repeat with w in windows
repeat with t in tabs of w
if (name of t) contains needle or (URL of t) contains needle then
set nm to name of t
close t
return nm
end if
end repeat
end repeat
end tell
return ""
end run"""


async def _close_tab(ctx: ToolContext, a: dict[str, Any]) -> str:
    b = await active_browser(ctx, a.get("browser"))
    needle = (a.get("match") or "").strip()
    out = await (ctx.osa(_CLOSE_TAB_SAFARI, needle) if b == "Safari" else ctx.osa(chromium_script(_CLOSE_TAB_CHROMIUM, b), needle))
    if not out:
        raise SkillError(f"I couldn't find a tab matching {needle}, sir.")
    return f"Closed {out}."


async def _close_all_tabs(ctx: ToolContext, a: dict[str, Any]) -> str:
    b = await active_browser(ctx, a.get("browser"))
    await ctx.osa(chromium_script('tell application "APPNAME" to close front window', b))
    return f"Closed the front {b} window with all its tabs."


# ---- quit everything ---------------------------------------------------------------------------------


async def _quit_all(ctx: ToolContext, a: dict[str, Any]) -> str:
    keep = KEEP_RUNNING | {x.strip() for x in (a.get("keep") or "").split(",") if x.strip()}
    victims = [app for app in await running_apps(ctx) if app not in keep]
    if not victims:
        return "There's nothing else open, sir."
    for app in victims:
        await ctx.osa("on run argv\ntell application (item 1 of argv) to quit\nend run", app)
    return f"Asked {len(victims)} apps to quit."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "take_screenshot", "Save a screenshot of the whole screen or the front window to the Screenshots folder.",
        obj({"target": E("screen or window", ["screen", "window"])}), _screenshot, category="capture"))
    reg.register(Tool(
        "set_focus", "Turn a Focus mode (Do Not Disturb, Work, Sleep...) on or off.",
        obj({"name": S("Focus name, default Do Not Disturb"), "state": E("on or off", ["on", "off"])}), _set_focus, category="focus"))
    reg.register(Tool(
        "make_shortcut", "Build one of Jarvis's helper shortcuts and open it in the Shortcuts app to add: "
        "'Turn Off Focus', 'Set Focus <Mode>'.",
        obj({"name": S("Shortcut name")}, ["name"]), _make_shortcut, category="focus"))
    reg.register(Tool(
        "switch_setting", "Turn Night Shift, True Tone or Stage Manager on or off.",
        obj({"setting": E("Which setting", ["night shift", "true tone", "stage manager"]),
             "state": E("on or off", ["on", "off"])}, ["setting", "state"]), _switch, category="display"))
    reg.register(Tool(
        "create_calendar_event", "Add an event to Apple Calendar, e.g. 'dentist tomorrow 4pm for 45 minutes'.",
        obj({
            "title": S("Event title"), "when": S("Natural-language start time"),
            "minutes": I("Duration in minutes, default 60"), "calendar": S("Optional calendar name"),
        }, ["title", "when"]), _event_create, category="calendar", timeout_s=45))
    reg.register(Tool(
        "delete_event", "Delete a calendar event in the next 30 days whose title contains the text.",
        obj({"title": S("Part of the event title")}, ["title"]), _event_delete,
        risk=Risk.CONFIRM, confirm_prompt=lambda a: f"Delete the calendar event matching {a.get('title')}?",
        category="calendar", timeout_s=70))
    reg.register(Tool(
        "delete_reminder", "Delete a reminder whose text contains the given words.",
        obj({"title": S("Part of the reminder text")}, ["title"]), _reminder_delete,
        risk=Risk.CONFIRM, confirm_prompt=lambda a: f"Delete the reminder matching {a.get('title')}?",
        category="reminders", timeout_s=50))
    browser = {"browser": S("Optional browser name")}
    reg.register(Tool(
        "close_tab", "Close one browser TAB (never an application: to close Safari, Chrome or any app use quit_app). "
        "The tab in front, or the first whose title or URL contains the text.",
        obj({"match": S("Optional text in the tab title or URL"), **browser}), _close_tab, category="browser"))
    reg.register(Tool(
        "close_all_tabs", "Close the front browser window with all of its tabs.",
        obj(browser), _close_all_tabs, risk=Risk.CONFIRM,
        confirm_prompt=lambda a: "Close the front browser window and all its tabs?", category="browser"))
    reg.register(Tool(
        "quit_all_apps", "Quit every open app except Finder and Jarvis.",
        obj({"keep": S("Optional comma-separated apps to leave open")}), _quit_all, risk=Risk.CONFIRM,
        confirm_prompt=lambda a: "Quit all your open apps?", category="apps", timeout_s=60))
