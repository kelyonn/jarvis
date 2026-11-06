"""Voice control over the alert queue: what did I miss, snooze, dismiss."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import N, Tool, ToolContext, ToolRegistry, obj


def _engine(ctx: ToolContext):
    e = ctx.services.get("alerts")
    if e is None:
        raise SkillError("The alert system isn't running, sir.")
    return e


async def _read_alerts(ctx: ToolContext, a: dict[str, Any]) -> str:
    eng = _engine(ctx)
    text = eng.summary()
    if len(eng.queue):
        eng.queue.clear()  # said aloud now, nothing left to hold
    return text


async def _clear_alerts(ctx: ToolContext, a: dict[str, Any]) -> str:
    n = _engine(ctx).dismiss_all()
    return "Cleared, sir." if n else "Nothing to clear, sir."


async def _snooze(ctx: ToolContext, a: dict[str, Any]) -> str:
    presence = ctx.services.get("presence")
    if presence is None:
        raise SkillError("The alert system isn't running, sir.")
    minutes = float(a.get("minutes", 60))
    if minutes <= 0:
        presence.unsnooze()
        return "Alerts resumed, sir."
    presence.snooze(minutes)
    if minutes >= 120 and minutes % 60 == 0:
        span = f"{int(minutes // 60)} hours"
    elif minutes >= 60 and minutes % 60 == 0:
        span = "an hour"
    else:
        span = f"{int(minutes)} minutes"
    return f"Understood, sir. I'll hold alerts for {span}."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "read_alerts",
        "Read out alerts that were held back (what the user missed). Read the returned text aloud as-is.",
        obj(), _read_alerts, category="alerts"))
    reg.register(Tool(
        "clear_alerts", "Dismiss all held alerts without reading them.", obj(), _clear_alerts, category="alerts"))
    reg.register(Tool(
        "snooze_alerts",
        "Hold proactive spoken alerts for a while ('don't disturb me for an hour'). minutes=0 resumes alerts.",
        obj({"minutes": N("How many minutes to hold alerts; 0 to resume")}, required=["minutes"]), _snooze,
        category="alerts"))
