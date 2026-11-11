"""Screen vision and narrated computer use."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import E, Risk, S, Tool, ToolContext, ToolRegistry, obj
from jarvis_core.vision.editor import CodeEditor


def _svc(ctx: ToolContext, name: str):
    s = ctx.services.get(name)
    if s is None:
        raise SkillError("Screen vision isn't available right now.")
    return s


async def _describe_screen(ctx: ToolContext, a: dict[str, Any]) -> str:
    q = (a.get("question") or "What is on the screen? Summarise it briefly.").strip()
    target = a.get("target") if a.get("target") in ("screen", "window") else "window"
    return await _svc(ctx, "vision").describe(q, target)


async def _computer_use(ctx: ToolContext, a: dict[str, Any]) -> str:
    cu = _svc(ctx, "computer_use")
    if cu.running:
        raise SkillError("I'm already in the middle of something.")
    result = await cu.run(a["goal"])
    return result.spoken()


def _confirm_cu(a: dict[str, Any]) -> str:
    goal = str(a.get("goal", "that")).rstrip(". ")
    return f"I'll take the mouse and keyboard to {goal}. Move the mouse or say stop to halt. Go ahead?"


def _editor(ctx: ToolContext) -> CodeEditor:
    ed = ctx.services.get("code_editor")
    if ed is None:
        ed = ctx.services["code_editor"] = CodeEditor(ctx, ctx.services.get("vision"))
    return ed


async def _read_screen_code(ctx: ToolContext, a: dict[str, Any]) -> str:
    return (await _editor(ctx).read()).report()


async def _edit_screen_code(ctx: ToolContext, a: dict[str, Any]) -> str:
    code = a.get("code")
    find = a.get("find")
    if not code and not find:
        raise SkillError("Give either the whole new code, or find and replace.")
    return await _editor(ctx).replace(code or None, find=find, replace=a.get("replace", ""))


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "describe_screen",
        "Look at the user's screen (only because they asked) and answer a question about it, e.g. 'explain this error', "
        "'what's on my screen', 'read this'. For code in an editor use read_screen_code instead. Screen contents are sent to the "
        "vision model.",
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
    reg.register(Tool(
        "read_screen_code",
        "Get the exact code in the user's code editor (LeetCode, HackerRank, VS Code, Xcode...) plus any test results or errors "
        "visible on screen. Call this before explaining, debugging or fixing code on their screen; never guess the code.",
        obj({}), _read_screen_code, category="vision", untrusted_output=True, timeout_s=60))
    reg.register(Tool(
        "edit_screen_code",
        "Change the code in the user's code editor directly. Pass `code` with the complete corrected program to replace "
        "everything, or `find` (exact existing text, unique) plus `replace` for a small edit. Keeps the user's own approach. "
        "Never runs or submits the code; Cmd+Z undoes it. Use this instead of computer_use for code edits.",
        obj({"code": S("The complete new code for the editor"),
             "find": S("Exact text currently in the editor to change"),
             "replace": S("What to put in its place")}),
        _edit_screen_code, category="vision", timeout_s=45, final=True))
