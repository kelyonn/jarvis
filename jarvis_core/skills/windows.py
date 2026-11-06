"""Window management through JarvisShell's Accessibility bridge."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.apps import target_app
from jarvis_core.skills.base import B, E, I, N, S, Tool, ToolContext, ToolRegistry, obj

POSITIONS = [
    "left", "right", "top", "bottom", "full", "center", "top_left", "top_right", "bottom_left",
    "bottom_right", "left_third", "middle_third", "right_third", "left_two_thirds", "right_two_thirds",
]

POSITION_WORDS = {
    "left half": "left", "right half": "right", "top half": "top", "bottom half": "bottom",
    "maximize": "full", "maximise": "full", "fullscreen": "full", "full screen": "full",
    "middle": "center", "centre": "center", "left side": "left", "right side": "right",
    "upper left": "top_left", "upper right": "top_right", "lower left": "bottom_left",
    "lower right": "bottom_right", "top left": "top_left", "top right": "top_right",
    "bottom left": "bottom_left", "bottom right": "bottom_right",
}


def normalise_position(p: str) -> str:
    p = p.strip().lower()
    if p in POSITIONS:
        return p
    p = POSITION_WORDS.get(p, p).replace(" ", "_")
    return p if p in POSITIONS else p


# ---- no shell app? System Events can still manage windows ------------------------------------------------------------

def _shell_missing(e: SkillError) -> bool:
    return "shell app" in str(e)


_PROC = 'tell application "System Events" to tell (first application process whose name is (item 1 of argv)) to '


async def _screen_frame(ctx: ToolContext) -> tuple[int, int, int, int]:
    """Usable area (x, y, w, h) of the main display, below the menu bar."""
    out = await ctx.osa('tell application "Finder" to get bounds of window of desktop')
    try:
        x1, y1, x2, y2 = (int(float(v)) for v in out.replace(" ", "").split(","))
    except ValueError:
        raise SkillError("I couldn't read the screen size, sir.") from None
    top = y1 + 25
    return x1, top, x2 - x1, y2 - top


def frame_for(position: str, area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, w, h = area
    hw, hh, tw = w // 2, h // 2, w // 3
    table = {
        "left": (x, y, hw, h), "right": (x + hw, y, hw, h), "top": (x, y, w, hh), "bottom": (x, y + hh, w, hh),
        "full": (x, y, w, h), "center": (x + w // 6, y + h // 6, w * 2 // 3, h * 2 // 3),
        "top_left": (x, y, hw, hh), "top_right": (x + hw, y, hw, hh), "bottom_left": (x, y + hh, hw, hh),
        "bottom_right": (x + hw, y + hh, hw, hh), "left_third": (x, y, tw, h), "middle_third": (x + tw, y, tw, h),
        "right_third": (x + 2 * tw, y, tw, h), "left_two_thirds": (x, y, tw * 2, h), "right_two_thirds": (x + tw, y, w - tw, h),
    }
    if position not in table:
        raise SkillError(f"I don't know the position {position}, sir.")
    return table[position]


async def _se_set_frame(ctx: ToolContext, app: str, x: float, y: float, w: float, h: float, index: int = 0) -> None:
    await ctx.osa(
        "on run argv\n" + _PROC + "tell window ((item 6 of argv as integer) + 1)\n"
        "set position to {(item 2 of argv as integer), (item 3 of argv as integer)}\n"
        "set size to {(item 4 of argv as integer), (item 5 of argv as integer)}\nend tell\nend run",
        app, str(int(x)), str(int(y)), str(int(w)), str(int(h)), str(index),
    )


async def _se_attr(ctx: ToolContext, app: str, attr: str, value: bool) -> None:
    await ctx.osa(
        "on run argv\n" + _PROC + f'set value of attribute "{attr}" of window 1 to {"true" if value else "false"}\nend run', app)


async def resolve_app(ctx: ToolContext, name: str) -> str:
    """Spoken app name, or "this window"/"it"/nothing meaning the app in front."""
    return await target_app(ctx, name or "")


async def snap_app(ctx: ToolContext, app: str, position: str, index: int = 0) -> str:
    app = await resolve_app(ctx, app)
    position = normalise_position(position or "full")
    try:
        await ctx.shell_request("ax.snap", {"app": app, "position": position, "index": index})
    except SkillError as e:
        if not _shell_missing(e):
            raise
        await _se_set_frame(ctx, app, *frame_for(position, await _screen_frame(ctx)), index=index)
    return f"{app} snapped {position.replace('_', ' ')}."


async def _snap(ctx: ToolContext, a: dict[str, Any]) -> str:
    return await snap_app(ctx, a["app"], a["position"], int(a.get("index") or 0))


async def _list(ctx: ToolContext, a: dict[str, Any]) -> str:
    args = {"app": await resolve_app(ctx, a["app"])} if a.get("app") else {}
    try:
        res = await ctx.shell_request("ax.list_windows", args)
        wins = res.get("windows", [])
    except SkillError as e:
        if not _shell_missing(e):
            raise
        out = await ctx.osa(
            'on run argv\nset res to ""\ntell application "System Events"\nrepeat with p in (every application process whose visible is true)\n'
            'if (item 1 of argv) is "" or (name of p) is (item 1 of argv) then\ntry\nrepeat with w in windows of p\n'
            'set res to res & (name of p) & "\t" & (name of w) & linefeed\nend repeat\nend try\nend if\nend repeat\nend tell\nreturn res\nend run',
            args.get("app", ""), timeout=30)
        wins = [{"app": a_, "title": "" if t_ == "missing value" else t_} for a_, _, t_ in (ln.partition("\t") for ln in out.splitlines() if ln.strip())]
    if not wins:
        return "No open windows."
    return "\n".join(f"{w['app']}: {w.get('title') or 'untitled'}" + (" (minimised)" if w.get("minimized") else "") for w in wins[:25])


async def _frontmost(ctx: ToolContext, a: dict[str, Any]) -> str:
    try:
        r = await ctx.shell_request("ax.frontmost")
    except SkillError as e:
        if not _shell_missing(e):
            raise
        out = await ctx.osa(
            'tell application "System Events"\nset p to first application process whose frontmost is true\n'
            'set t to ""\ntry\nset t to name of window 1 of p\nend try\nreturn (name of p) & "\t" & t\nend tell')
        name, _, title = out.partition("\t")
        r = {"app": name, "window": "" if title == "missing value" else title}
    return f"{r.get('app', 'unknown')}: {r.get('window', '')}".strip(": ")


async def _minimize(ctx: ToolContext, a: dict[str, Any]) -> str:
    app = await resolve_app(ctx, a.get("app") or "")
    try:
        await ctx.shell_request("ax.minimize", {"app": app})
    except SkillError as e:
        if not _shell_missing(e):
            raise
        await _se_attr(ctx, app, "AXMinimized", True)
    return f"Minimised {app}."


async def _unminimize(ctx: ToolContext, a: dict[str, Any]) -> str:
    app = await resolve_app(ctx, a["app"])
    try:
        await ctx.shell_request("ax.unminimize", {"app": app})
    except SkillError as e:
        if not _shell_missing(e):
            raise
        await _se_attr(ctx, app, "AXMinimized", False)
        await ctx.run(["open", "-a", app])
    return f"Restored {app}."


async def _fullscreen(ctx: ToolContext, a: dict[str, Any]) -> str:
    app = await resolve_app(ctx, a["app"])
    on = a.get("on", True)
    try:
        await ctx.shell_request("ax.fullscreen", {"app": app, "on": bool(on)})
    except SkillError as e:
        if not _shell_missing(e):
            raise
        await _se_attr(ctx, app, "AXFullScreen", bool(on))
    return f"{app} is {'now' if on else 'no longer'} full screen."


async def _close_window(ctx: ToolContext, a: dict[str, Any]) -> str:
    """Close the front window of an app (the app keeps running)."""
    app = await resolve_app(ctx, a.get("app") or "")
    try:
        await ctx.shell_request("ax.close_window", {"app": app})
    except SkillError:
        await ctx.osa(
            "on run argv\n" + _PROC + 'perform action "AXPress" of (first button of window 1 whose subrole is "AXCloseButton")\nend run', app)
    return f"Closed the {app} window."


async def _move_resize(ctx: ToolContext, a: dict[str, Any]) -> str:
    app = await resolve_app(ctx, a["app"])
    args = {k: a[k] for k in ("x", "y", "w", "h") if k in a}
    try:
        await ctx.shell_request("ax.set_frame", {"app": app, **args})
    except SkillError as e:
        if not _shell_missing(e):
            raise
        cur = await ctx.osa('on run argv\n' + _PROC + 'get (position of window 1) & (size of window 1)\nend run', app)
        x0, y0, w0, h0 = (float(v) for v in cur.replace(" ", "").split(","))
        await _se_set_frame(ctx, app, args.get("x", x0), args.get("y", y0), args.get("w", w0), args.get("h", h0))
    return f"Adjusted {app}."


def register(reg: ToolRegistry) -> None:
    app = {"app": S("App name")}
    reg.register(Tool(
        "window_snap", "Snap an app's window to part of the screen.",
        obj({**app, "position": E("Where to place it", POSITIONS), "index": I("Window number, default 0")}, ["app", "position"]),
        _snap, category="windows"))
    reg.register(Tool("list_windows", "List open windows, optionally for one app.", obj({"app": S("Optional app name")}), _list, category="windows"))
    reg.register(Tool("frontmost_window", "Which app and window is in front.", obj(), _frontmost, category="windows"))
    reg.register(Tool(
        "window_close", "Close the front window of an app (not the app itself). Omit 'app' for the window in front. Use for 'close this window'.",
        obj({"app": S("Optional app name; default is the app in front")}), _close_window, category="windows"))
    reg.register(Tool("window_minimize", "Minimise an app's window.", obj({"app": S("App name; omit for the app in front")}), _minimize, category="windows"))
    reg.register(Tool("window_restore", "Un-minimise an app's window.", obj(app, ["app"]), _unminimize, category="windows"))
    reg.register(Tool("window_fullscreen", "Toggle full screen for an app's window.", obj({**app, "on": B("true to enter, false to leave")}, ["app"]), _fullscreen, category="windows"))
    reg.register(Tool(
        "window_move_resize", "Place a window at exact coordinates (points, top-left origin).",
        obj({**app, "x": N("x"), "y": N("y"), "w": N("width"), "h": N("height")}, ["app"]),
        _move_resize, category="windows"))
