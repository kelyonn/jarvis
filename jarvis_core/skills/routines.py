"""Routines: named sets of apps + window layout (+ optional Focus shortcut)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rapidfuzz import fuzz, process

from jarvis_core import config as cfgmod
from jarvis_core.config import RoutineCfg
from jarvis_core.errors import SkillError
from jarvis_core.frontend import FrontendError
from jarvis_core.skills.apps import resolve_app
from jarvis_core.skills.base import S, Tool, ToolContext, ToolRegistry, obj
from jarvis_core.skills.windows import normalise_position, snap_app

log = logging.getLogger(__name__)

WINDOW_WAIT_S = 8.0


def find_routine(ctx: ToolContext, name: str) -> str:
    names = list(ctx.cfg.routines)
    if not names:
        raise SkillError("You haven't set up any routines yet, sir.")
    key = name.strip().lower()
    for n in names:
        if n.lower() == key:
            return n
    best = process.extractOne(key, names, scorer=fuzz.WRatio)
    if best and best[1] >= 70:
        return best[0]
    raise SkillError(f"I don't have a routine called {name}, sir. I have {', '.join(names)}.")


async def _wait_for_window(ctx: ToolContext, app: str) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WINDOW_WAIT_S
    while loop.time() < deadline:
        try:
            res = await ctx.shell_request("ax.list_windows", {"app": app}, timeout=4)
            if res.get("windows"):
                return True
        except (SkillError, FrontendError):
            pass
        await asyncio.sleep(0.5)
    return False


async def run_routine_by_name(ctx: ToolContext, name: str) -> str:
    key = find_routine(ctx, name)
    r = ctx.cfg.routines[key]
    opened: list[str] = []
    for a in r.apps:
        app = resolve_app(ctx, a)
        res = await ctx.run(["open", "-a", app])
        if res.ok:
            opened.append(app)
        else:
            log.warning("routine %s: could not open %s", key, app)
    notes: list[str] = []
    if r.layout:
        shell_up = ctx.frontend is not None and getattr(ctx.frontend, "connected", False)
        if not shell_up:
            notes.append("I skipped the window layout because the shell isn't running")
        else:
            for a, pos in r.layout.items():
                app = resolve_app(ctx, a)
                if await _wait_for_window(ctx, app):
                    try:
                        await snap_app(ctx, app, normalise_position(pos))
                    except SkillError as e:
                        notes.append(str(e))
                else:
                    notes.append(f"{app} didn't open a window in time")
    if r.focus:
        from jarvis_core.adapters import shortcuts as shortcuts_kit
        from jarvis_core.errors import ShortcutMissing

        try:
            await shortcuts_kit.run(ctx, r.focus)
        except ShortcutMissing:
            shortcuts_kit.want(ctx, r.focus)
            notes.append(f"the {r.focus} shortcut isn't set up")
        except SkillError:
            notes.append(f"I couldn't start the {r.focus} focus")
    msg = f"{key} is ready: opened {', '.join(opened) if opened else 'nothing'}."
    return msg + (" " + "; ".join(notes) + "." if notes else "")


async def _run(ctx: ToolContext, a: dict[str, Any]) -> str:
    return await run_routine_by_name(ctx, a["name"])


async def _list(ctx: ToolContext, a: dict[str, Any]) -> str:
    if not ctx.cfg.routines:
        return "No routines yet."
    return "\n".join(f"{n}: {', '.join(r.apps) or 'no apps'}" for n, r in ctx.cfg.routines.items())


async def _create(ctx: ToolContext, a: dict[str, Any]) -> str:
    name = a["name"].strip().lower()
    apps = [x.strip() for x in a["apps"].split(",") if x.strip()]
    if not apps:
        raise SkillError("Which apps should it open, sir?")
    layout: dict[str, str] = {}
    for part in (a.get("layout") or "").split(","):
        if "=" in part:
            app, pos = part.split("=", 1)
            layout[app.strip()] = normalise_position(pos)
    routine = RoutineCfg(apps=apps, layout=layout, focus=a.get("focus") or None)

    def mut(c: cfgmod.Config) -> None:
        c.routines[name] = routine

    new = cfgmod.update_config(mut)
    cfgmod.reload_into(ctx.cfg, new)
    return f"Routine {name} saved with {len(apps)} apps."


async def _delete(ctx: ToolContext, a: dict[str, Any]) -> str:
    key = find_routine(ctx, a["name"])

    def mut(c: cfgmod.Config) -> None:
        c.routines.pop(key, None)

    cfgmod.reload_into(ctx.cfg, cfgmod.update_config(mut))
    return f"Deleted routine {key}."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool("run_routine", "Run a saved routine (open its apps and arrange windows).", obj({"name": S("Routine name, e.g. 'work'")}, ["name"]), _run, category="routines", timeout_s=60))
    reg.register(Tool("list_routines", "List saved routines.", obj(), _list, category="routines"))
    reg.register(Tool(
        "create_routine", "Save a routine: comma-separated apps, optional layout like 'Cursor=left, Arc=right', optional Focus shortcut name.",
        obj({"name": S("Routine name"), "apps": S("Comma-separated app names"), "layout": S("Optional 'App=position' pairs"), "focus": S("Optional Shortcuts name that enables a Focus")}, ["name", "apps"]),
        _create, category="routines"))
    reg.register(Tool("delete_routine", "Delete a saved routine.", obj({"name": S("Routine name")}, ["name"]), _delete, category="routines"))


def routines_cli(action: str, name: str | None) -> int:
    cfg = cfgmod.load_config()
    if action == "list":
        if not cfg.routines:
            print("No routines.")
        for n, r in cfg.routines.items():
            print(f"{n}: apps={r.apps} layout={r.layout} focus={r.focus}")
        return 0
    if action == "delete":
        if not name or name not in cfg.routines:
            print(f"No routine named {name!r}.")
            return 1

        def mut(c: cfgmod.Config) -> None:
            c.routines.pop(name, None)

        cfgmod.update_config(mut)
        print(f"Deleted {name}.")
        return 0
    return 2
