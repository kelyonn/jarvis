"""Reminders due today or overdue (Apple Reminders via AppleScript)."""

from __future__ import annotations

from jarvis_core.briefing.env import Env
from jarvis_core.errors import SkillError

_SCRIPT = """set res to ""
set cutoff to (current date) + 1 * days
set cutoff's hours to 0
set cutoff's minutes to 0
set cutoff's seconds to 0
tell application "Reminders"
repeat with r in (reminders whose completed is false)
set d to due date of r
if d is not missing value and d < cutoff then
set res to res & (name of r) & linefeed
end if
end repeat
end tell
return res"""


async def collect(env: Env) -> list[str]:
    if env.ctx is None:
        raise SkillError("no system access")
    out = await env.ctx.osa(_SCRIPT, timeout=30)
    return [ln.strip() for ln in out.splitlines() if ln.strip()][:10]


def describe(items: list[str]) -> str:
    if not items:
        return "No reminders due."
    return f"{len(items)} reminder{'s' if len(items) != 1 else ''} due: " + "; ".join(items[:5]) + "."
