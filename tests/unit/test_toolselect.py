from __future__ import annotations

import json

import pytest
from fakes import FakeRunner

from jarvis_core.assistant import Assistant
from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.brain.toolselect import CORE, match_categories, select_categories
from jarvis_core.config import Config
from jarvis_core.interfaces import ToolCallEvent
from jarvis_core.skills.base import ToolContext
from jarvis_core.skills.registry import build_registry


@pytest.mark.parametrize("text,expected", [
    ("reply to mom that I'll call at eight", "messaging"),
    ("snap chrome to the left half", "windows"),
    ("play some jazz", "media"),
    ("what's the weather and open my calendar", "briefing"),
    ("remind me to call Rahul", "reminders"),
    ("set a timer for ten minutes", "timers"),
    ("connect my airpods", "connectivity"),
    ("what's on my screen", "vision"),
    ("run git status", "terminal"),
    ("start work mode", "routines"),
    ("what did I miss", "alerts"),
    ("turn on dark mode", "display"),
    ("how much battery is left", "power"),
    ("move that file to the trash", "files"),
    ("open a new tab", "browser"),
    ("what's on my clipboard", "clipboard"),
    ("create a note about groceries", "notes"),
])
def test_keywords_route_to_categories(text, expected):
    assert expected in match_categories(text), text
    sel = select_categories(text)
    assert CORE <= sel and expected in sel


def test_multi_intent_and_fallbacks():
    sel = select_categories("what's the weather and snap chrome left")
    assert {"briefing", "windows"} <= sel
    assert select_categories("tidy things up") is None  # nothing matched: offer everything
    assert select_categories("") is None and select_categories("   ") is None
    assert "messaging" in select_categories("make it shorter, and play music", sticky={"messaging"})


def test_selection_shrinks_the_prompt():
    reg = build_registry()
    full = len(json.dumps(reg.schemas()))
    some = len(json.dumps(reg.schemas(select_categories("play some jazz"))))
    assert some < full * 0.4
    names = {s["function"]["name"] for s in reg.schemas(select_categories("play some jazz"))}
    assert "play_music" in names and "open_app" in names and "remember" in names and "delete_routine" not in names


def make_assistant(replies, cfg=None):
    cfg = cfg or Config()
    llm = ScriptedLLM(replies)
    ctx = ToolContext(cfg=cfg, runner=FakeRunner())
    return Assistant.create(cfg, llm=llm, ctx=ctx, router=None), llm


def offered(llm, i):
    return {s["function"]["name"] for s in llm.calls[i]["tools"]}


async def test_agent_offers_a_subset_then_sticks_to_it():
    a, llm = make_assistant([
        [ToolCallEvent("1", "current_time", {})],
        "It is late, sir.",
        "Of course, sir.",
        "Very good.",
    ])
    text = "".join([c async for c in a.respond_stream("what is the volume level and the time")])
    assert text
    first = offered(llm, 0)
    assert "get_volume" in first and "delete_routine" not in first and len(first) < len(a.registry)
    # a follow-up with no keywords of its own still sees the media tools used a moment ago
    [c async for c in a.respond_stream("a bit lower then")]
    assert "change_volume" in offered(llm, 2)
    # unrecognised request: everything is offered
    [c async for c in a.respond_stream("tidy things up")]
    assert len(offered(llm, 3)) == len(a.registry)


async def test_selection_can_be_turned_off():
    cfg = Config()
    cfg.llm.tool_selection = False
    a, llm = make_assistant(["Fine."], cfg)
    [c async for c in a.respond_stream("play some jazz")]
    assert len(offered(llm, 0)) == len(a.registry)
