"""Screen vision and narrated computer use."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import E, Risk, S, Tool, ToolContext, ToolRegistry, obj


def _svc(ctx: ToolContext, name: str):
    s = ctx.services.get(name)
    if s is None:
        raise SkillError("Screen vision isn't available right now, sir.")
    return s


async def _describe_screen(ctx: ToolContext, a: dict[str, Any]) -> str:
    q = (a.get("question") or "What is on the screen? Summarise it briefly.").strip()
    target = a.get("target") if a.get("target") in ("screen", "window") else "window"
    return await _svc(ctx, "vision").describe(q, target)


async def _computer_use(ctx: ToolContext, a: dict[str, Any]) -> str:
    cu = _svc(ctx, "computer_use")
    if cu.running:
        raise SkillError("I'm already in the middle of something, sir.")
    result = await cu.run(a["goal"])
    return result.spoken()


def _confirm_cu(a: dict[str, Any]) -> str:
    goal = str(a.get("goal", "that")).rstrip(". ")
    return (f"To {goal}, I'll take over the mouse and keyboard, sir. Move the mouse or say stop and I'll halt immediately. "
            "Shall I proceed?")


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "describe_screen",
        "Look at the user's screen (only because they asked) and answer a question about it, e.g. 'explain this error', "
        "'what's on my screen', 'read this'. Screen contents are sent to the vision model.",
        obj({"question": S("What to find out about the screen"),
             "target": E("The front window or the whole screen", ["window", "screen"])}),
        _describe_screen, category="vision", untrusted_output=True, timeout_s=45))
    reg.register(Tool(
        "computer_use",
        "Operate the Mac by looking at the screen and using mouse and keyboard, step by step, narrating as it goes. "
        "For tasks no other tool covers (clicking through an app, filling a form). Never sends, posts, buys or deletes; "
        "the user does the final step. The user can stop it any time.",
        obj({"goal": S("What to accomplish, in one sentence")}, required=["goal"]),
        _computer_use, risk=Risk.CONFIRM, confirm_prompt=_confirm_cu, category="vision", timeout_s=200))
