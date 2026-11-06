"""Smaller everyday abilities: copy files, jump to a System Settings pane, show a notification."""

from __future__ import annotations

import shutil
from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import S, Tool, ToolContext, ToolRegistry, obj
from jarvis_core.skills.files import _guard, resolve_path

SETTINGS_PANES = {
    "wifi": "com.apple.wifi-settings-extension", "network": "com.apple.Network-Settings.extension",
    "bluetooth": "com.apple.BluetoothSettings", "display": "com.apple.Displays-Settings.extension",
    "displays": "com.apple.Displays-Settings.extension", "sound": "com.apple.Sound-Settings.extension",
    "battery": "com.apple.Battery-Settings.extension", "notifications": "com.apple.Notifications-Settings.extension",
    "keyboard": "com.apple.Keyboard-Settings.extension", "trackpad": "com.apple.Trackpad-Settings.extension",
    "mouse": "com.apple.Mouse-Settings.extension", "general": "com.apple.systempreferences.GeneralSettings",
    "storage": "com.apple.settings.Storage", "software update": "com.apple.Software-Update-Settings.extension",
    "wallpaper": "com.apple.Wallpaper-Settings.extension", "privacy": "com.apple.settings.PrivacySecurity.extension",
    "security": "com.apple.settings.PrivacySecurity.extension", "accessibility": "com.apple.Accessibility-Settings.extension",
    "focus": "com.apple.Focus-Settings.extension", "control center": "com.apple.ControlCenter-Settings.extension",
    "users": "com.apple.Users-Groups-Settings.extension", "login items": "com.apple.LoginItems-Settings.extension",
    "printers": "com.apple.Print-Scan-Settings.extension", "date and time": "com.apple.Date-Time-Settings.extension",
    "siri": "com.apple.Siri-Settings.extension", "apple id": "com.apple.systempreferences.AppleIDSettings",
}


async def _copy_file(ctx: ToolContext, a: dict[str, Any]) -> str:
    src = _guard(ctx, resolve_path(ctx, a["source"]))
    dst = _guard(ctx, resolve_path(ctx, a["destination"]))
    if not src.exists():
        raise SkillError(f"I can't find {src.name}, sir.")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists():
        raise SkillError(f"{dst.name} already exists there, sir. I won't overwrite it.")
    if not dst.parent.exists():
        raise SkillError("That destination folder doesn't exist, sir.")
    (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
    return f"Copied {src.name} to {dst.parent}."


async def _open_settings(ctx: ToolContext, a: dict[str, Any]) -> str:
    name = (a.get("pane") or "").strip().lower().removesuffix(" settings").removesuffix(" preferences")
    pane = SETTINGS_PANES.get(name)
    if pane is None:
        if not name:
            res = await ctx.run(["open", "-a", "System Settings"])
            return "Opened System Settings." if res.ok else "I couldn't open System Settings, sir."
        raise SkillError(f"I don't know a settings pane called {a['pane']}, sir. Try: {', '.join(sorted(SETTINGS_PANES)[:8])}.")
    res = await ctx.run(["open", f"x-apple.systempreferences:{pane}"])
    if not res.ok:
        raise SkillError("I couldn't open that settings pane, sir.")
    return f"Opened {name} settings."


async def _notify(ctx: ToolContext, a: dict[str, Any]) -> str:
    await ctx.osa(
        "on run argv\ndisplay notification (item 1 of argv) with title (item 2 of argv)\nend run",
        a["text"].strip()[:200], (a.get("title") or "Jarvis").strip()[:60])
    return "Notification shown."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "copy_file", "Copy a file or folder to another place. Never overwrites an existing file.",
        obj({"source": S("File or folder to copy"), "destination": S("Destination folder or new path")}, ["source", "destination"]),
        _copy_file, category="files"))
    reg.register(Tool(
        "open_settings", "Open System Settings, optionally at a pane: wifi, bluetooth, display, sound, battery, notifications, "
        "keyboard, privacy, storage, software update, wallpaper, focus, accessibility, users, login items...",
        obj({"pane": S("Pane name, e.g. 'bluetooth' or 'privacy'; omit to open System Settings")}), _open_settings, category="apps"))
    reg.register(Tool(
        "notify", "Show a macOS notification banner with the given text.",
        obj({"text": S("Notification text"), "title": S("Optional title")}, ["text"]), _notify, category="sysinfo"))
