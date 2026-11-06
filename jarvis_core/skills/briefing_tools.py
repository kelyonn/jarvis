"""Tools that expose the briefing system to the agent and the fast router."""

from __future__ import annotations

from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.base import E, I, S, Tool, ToolContext, ToolRegistry, obj


def _svc(ctx: ToolContext):
    svc = ctx.services.get("briefing")
    if svc is None:
        raise SkillError("The briefing service isn't running, sir.")
    return svc


async def _brief_me(ctx: ToolContext, a: dict[str, Any]) -> str:
    mode = a.get("mode") or "auto"
    return await _svc(ctx).text(mode)


async def _weather(ctx: ToolContext, a: dict[str, Any]) -> str:
    return await _svc(ctx).weather_text(a.get("city"), tomorrow=a.get("day") == "tomorrow")


async def _markets(ctx: ToolContext, a: dict[str, Any]) -> str:
    syms = [s.strip() for s in (a.get("symbols") or "").split(",") if s.strip()] or None
    return await _svc(ctx).markets_text(syms)


async def _news(ctx: ToolContext, a: dict[str, Any]) -> str:
    return await _svc(ctx).news_text(a.get("topic") or "world")


async def _calendar(ctx: ToolContext, a: dict[str, Any]) -> str:
    return await _svc(ctx).calendar_text(1 if a.get("day") == "tomorrow" else 0)


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "brief_me",
        "Give the user their briefing (weather, calendar, email, messages, markets, news). "
        "'delta' covers only what changed since the last brief. Read the returned text aloud as-is.",
        obj({"mode": E("auto, full or delta", ["auto", "full", "delta"])}), _brief_me,
        category="briefing", untrusted_output=False, timeout_s=60))
    reg.register(Tool(
        "get_weather", "Weather now, today or tomorrow, for the user's location or a named city.",
        obj({"city": S("Optional city name"), "day": E("today or tomorrow", ["today", "tomorrow"])}), _weather,
        category="briefing", timeout_s=20))
    reg.register(Tool(
        "get_markets", "Current market levels: the configured indices and crypto, or given Yahoo symbols (comma-separated).",
        obj({"symbols": S("Optional comma-separated Yahoo Finance symbols, e.g. 'AAPL,^NSEI'")}), _markets,
        category="briefing", timeout_s=20))
    reg.register(Tool(
        "get_news", "Top headlines for a topic.",
        obj({"topic": E("world, india or tech", ["world", "india", "tech"])}), _news,
        category="briefing", untrusted_output=True, timeout_s=25))
    reg.register(Tool(
        "get_calendar", "The user's calendar events for today or tomorrow.",
        obj({"day": E("today or tomorrow", ["today", "tomorrow"])}), _calendar,
        category="briefing", untrusted_output=True, timeout_s=20))
    _ = I
