"""Shortcuts Jarvis relies on for things macOS won't let other apps do (Focus, Night Shift, True Tone, Stage Manager).

Each recipe builds a workflow plist. ``install`` signs it with ``shortcuts sign`` and opens it, so the Shortcuts app
offers "Add Shortcut" (macOS never lets an app add one silently). ``run`` raises ``ShortcutMissing`` when a shortcut
isn't installed, and the agent then offers to make it.
"""

from __future__ import annotations

import plistlib
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jarvis_core import paths
from jarvis_core.errors import ShortcutMissing, SkillError

FOCUS_OFF = "Turn Off Focus"

# Focus name -> system identifier. Custom Focus modes have per-Mac ids, so they can't be built here.
FOCUS_MODES = {
    "Do Not Disturb": "com.apple.donotdisturb.mode.default",
    "Work": "com.apple.focus.work",
    "Personal": "com.apple.focus.personal-time",
    "Sleep": "com.apple.sleep.sleep-mode",
    "Reading": "com.apple.focus.reading",
    "Gaming": "com.apple.focus.gaming",
    "Fitness": "com.apple.focus.fitness",
    "Mindfulness": "com.apple.focus.mindfulness",
    "Driving": "com.apple.donotdisturb.mode.driving",
}

# On/off switches macOS only exposes to Shortcuts: setting -> action identifier.
SWITCHES = {
    "Night Shift": "is.workflow.actions.nightshift.set",
    "True Tone": "is.workflow.actions.truetone.set",
    "Stage Manager": "is.workflow.actions.stagemanager.set",
}


def switch_shortcut(setting: str, on: bool) -> str:
    return f"Jarvis {setting} {'On' if on else 'Off'}"


# Built by `jarvisd shortcuts install` with no names.
DEFAULT_SET = ["Set Focus Do Not Disturb", FOCUS_OFF,
               *(switch_shortcut(k, on) for k in SWITCHES for on in (True, False))]


def focus_shortcut(name: str) -> str:
    return f"Set Focus {name}"


def _wf(actions: list[dict[str, Any]], *, takes_input: bool = False) -> dict[str, Any]:
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "2700",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 463140863, "WFWorkflowIconGlyphNumber": 59771},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"] if takes_input else [],
        "WFWorkflowTypes": [],
    }


def _focus_on(name: str, ident: str) -> dict[str, Any]:
    return _wf([{
        "WFWorkflowActionIdentifier": "is.workflow.actions.dnd.set",
        "WFWorkflowActionParameters": {
            "Enabled": 1, "FocusModes": {"Identifier": ident, "DisplayString": name}, "AssertionType": "Turn On",
        },
    }])


def _focus_off() -> dict[str, Any]:
    return _wf([{
        "WFWorkflowActionIdentifier": "is.workflow.actions.dnd.set",
        "WFWorkflowActionParameters": {"Enabled": 0, "AssertionType": "Turn Off"},
    }])


def _switch(action: str, on: bool) -> dict[str, Any]:
    return _wf([{"WFWorkflowActionIdentifier": action, "WFWorkflowActionParameters": {"OnValue": on}}])


def recipe(name: str) -> Callable[[], dict[str, Any]] | None:
    """The builder for a shortcut Jarvis knows how to make, or None."""
    fixed: dict[str, Callable[[], dict[str, Any]]] = {
        FOCUS_OFF: _focus_off,
    }
    if name in fixed:
        return fixed[name]
    for setting, action in SWITCHES.items():
        for on in (True, False):
            if name.lower() == switch_shortcut(setting, on).lower():
                return lambda a=action, o=on: _switch(a, o)
    for mode, ident in FOCUS_MODES.items():
        if name.lower() == focus_shortcut(mode).lower():
            return lambda m=mode, i=ident: _focus_on(m, i)
    return None


def can_build(name: str) -> bool:
    return recipe(name) is not None


def want(ctx: Any, name: str) -> None:
    """Note that a tool needed ``name``; the agent asks whether to make it (once per name per session)."""
    asked = ctx.state.setdefault("shortcuts_offered", set())
    if name not in asked and can_build(name):
        asked.add(name)
        ctx.state["shortcut_wanted"] = name


def take_wanted(ctx: Any) -> str | None:
    return ctx.state.pop("shortcut_wanted", None)


def shortcuts_dir() -> Path:
    d = paths.sub("shortcuts")
    d.mkdir(parents=True, exist_ok=True)
    return d


async def installed(ctx: Any) -> set[str]:
    res = await ctx.run(["shortcuts", "list"], timeout=10)
    if not res.ok:
        raise SkillError("I couldn't reach the Shortcuts app, sir.")
    return {line.strip() for line in res.out.splitlines() if line.strip()}


async def run(ctx: Any, name: str, input_text: str | None = None, *, timeout: float = 20) -> None:
    """Run an installed shortcut. Raises ShortcutMissing if it isn't there, SkillError if it fails."""
    have = await installed(ctx)
    match = next((n for n in have if n.lower() == name.lower()), None)
    if match is None:
        raise ShortcutMissing(name, can_build(name))
    argv = ["shortcuts", "run", match]
    tmp: Path | None = None
    if input_text is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(input_text)
            tmp = Path(f.name)
        argv += ["-i", str(tmp)]
    try:
        res = await ctx.run(argv, timeout=timeout)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    if not res.ok:
        raise SkillError(f"The '{match}' shortcut failed, sir: {(res.err or res.out or 'no detail')[:120]}")


def build_file(name: str, out_dir: Path) -> Path:
    """Write the unsigned workflow for ``name``; returns its path."""
    make = recipe(name)
    if make is None:
        raise SkillError(f"I don't know how to build a '{name}' shortcut, sir.")
    raw = out_dir / f"{name}.unsigned.shortcut"
    raw.write_bytes(plistlib.dumps(make(), fmt=plistlib.FMT_BINARY))
    return raw


async def install(ctx: Any, name: str) -> str:
    """Build, sign and open the shortcut so the Shortcuts app offers to add it."""
    out = shortcuts_dir()
    raw = build_file(name, out)
    signed = out / f"{name}.shortcut"
    try:
        res = await ctx.run(
            ["shortcuts", "sign", "--mode", "anyone", "--input", str(raw), "--output", str(signed)], timeout=60)
    finally:
        raw.unlink(missing_ok=True)
    if not res.ok:
        raise SkillError(f"I couldn't sign the '{name}' shortcut, sir.")
    opened = await ctx.run(["open", str(signed)], timeout=10)
    if not opened.ok:
        raise SkillError(f"I built the '{name}' shortcut but couldn't open it, sir. It's in {out}.")
    return (f"I've opened the '{name}' shortcut in the Shortcuts app, sir. Click Add Shortcut, "
            "then ask me again.")
