"""The one place that decides whether a tool call may run.

Every path that executes a tool (the agent loop, the router's direct calls, the draft shortcut, the dev hook) asks
:func:`check` first, so a capability switched off in ``config.toml`` is off everywhere and a ``block`` verdict can never
be skipped by a side door.
"""

from __future__ import annotations

from typing import Any

from jarvis_core.skills.base import Decision, Tool, ToolContext

# Capability names a user can switch off (``safety.disabled_capabilities``) and the tool categories each one covers.
# A name that is not listed is treated as a category name itself, so ``"mcp"`` works as soon as MCP tools exist.
CAPABILITIES: dict[str, frozenset[str]] = {
    "shell": frozenset({"terminal"}),
    "browser": frozenset({"browser", "web"}),
    "screen": frozenset({"vision", "capture", "ui"}),
    "camera": frozenset({"camera"}),
    "files": frozenset({"files"}),
    "messaging": frozenset({"messaging"}),
}


def disabled_categories(cfg: Any) -> set[str]:
    names = getattr(getattr(cfg, "safety", None), "disabled_capabilities", None) or []
    out: set[str] = set()
    for name in names:
        key = str(name).strip().lower()
        if key:
            out |= CAPABILITIES.get(key, frozenset({key}))
    return out


def check(tool: Tool, ctx: ToolContext, args: dict[str, Any]) -> Decision:
    """Allow, ask for a spoken yes, or block. Capability switches come first, then the tool's own risk rule."""
    if tool.category and tool.category in disabled_categories(ctx.cfg):
        return Decision.block(f"{tool.category} is turned off in my settings")
    return tool.decide(ctx, args)
