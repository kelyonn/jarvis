from __future__ import annotations

from datetime import datetime

import pytest
from fakes import FakeRunner

from jarvis_core.config import Config
from jarvis_core.policy.audit import AuditLog
from jarvis_core.router import IntentRouter
from jarvis_core.skills.base import ToolContext
from jarvis_core.skills.registry import build_registry


def _ctx(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return ToolContext(
        cfg=Config(), runner=FakeRunner(), audit=AuditLog(tmp_path / "audit.log"), app_dirs=[], home_dir=home,
        clock=lambda: datetime(2026, 11, 4, 12, 0).astimezone(),
    )


@pytest.mark.parametrize("text,time", [
    ("Set an alarm at 6 pm.", "6 pm"), ("set an alarm for 6 pm", "6 pm"), ("wake me up at 7 30 am", "7 30 am"),
])
def test_alarm_routes_to_alarm_set(text, time):
    m = IntentRouter.load().match(text)
    assert m and m.tool == "alarm_set" and m.args["time"] == time


@pytest.mark.parametrize("text,person,app", [
    ("open my whatsapp chat with Naan", "naan", "whatsapp"),
    ("open naan's chat on whatsapp", "naan", "whatsapp"),
    ("open Rahul on WhatsApp", "rahul", "whatsapp"),
])
def test_open_chat_beats_open_app(text, person, app):
    m = IntentRouter.load().match(text)
    assert m and m.tool == "open_chat" and m.args["person"] == person and m.args["app"] == app


def test_plain_open_app_still_works():
    assert IntentRouter.load().match("open whatsapp").tool == "open_app"


@pytest.mark.asyncio
@pytest.mark.parametrize("when,hour", [("6 pm", 18), ("7 30 am", 7), ("6", 18)])
async def test_alarm_set_picks_future_wall_clock_time(tmp_path, when, hour):
    ctx = _ctx(tmp_path)
    out = await build_registry().get("alarm_set").handler(ctx, {"time": when})
    assert out.startswith("Alarm set for") and "minutes" not in out
    t = next(iter(ctx.state["timers"].values()))
    assert t["kind"] == "alarm" and t["ends"].hour == hour
    t["task"].cancel()


@pytest.mark.parametrize("text,tool", [
    ("open youtube", "open_site"), ("open gmail", "open_site"), ("open github.com", "open_url"),
    ("open my downloads folder", "open_path"), ("go to my desktop", "open_path"),
    ("close this tab", "close_tab"), ("close the window", "window_close"), ("minimize the window", "window_minimize"),
    ("quit all apps", "quit_all_apps"), ("switch to the github tab", "browser_focus_tab"),
    ("turn wifi off", "wifi"), ("turn bluetooth off", "bluetooth"), ("weather in mumbai", "get_weather"),
    ("remind me to call mom at 5 pm", "reminder_create"), ("remind me in 20 minutes to stretch", "reminder_create"),
    ("add a note buy milk", "note_create"), ("list my reminders", "reminder_list"), ("pause", "media_control"),
    ("what is eating my cpu", "top_processes"),
])
def test_fast_paths(text, tool):
    m = IntentRouter.load().match(text)
    assert m and m.tool == tool, (text, m and m.tool)


def test_pause_does_not_toggle_and_resume_plays():
    r = IntentRouter.load()
    assert r.match("pause").args == {"action": "pause"}
    assert r.match("resume music").args == {"action": "play"}


@pytest.mark.parametrize("name", ["this tab", "the window", "apps", "everything", "this"])
def test_generic_words_never_resolve_to_an_app(tmp_path, name):
    from jarvis_core.errors import SkillError
    from jarvis_core.skills.apps import resolve_app

    with pytest.raises(SkillError):
        resolve_app(_ctx(tmp_path), name)


@pytest.mark.asyncio
async def test_copy_file_never_overwrites_or_touches_secrets(tmp_path):
    from jarvis_core.errors import SkillError

    ctx = _ctx(tmp_path)
    (ctx.home_dir / "a.txt").write_text("hi")
    (ctx.home_dir / "docs").mkdir()
    copy = build_registry().get("copy_file").handler
    assert "Copied" in await copy(ctx, {"source": "a.txt", "destination": "docs"})
    assert (ctx.home_dir / "docs/a.txt").read_text() == "hi"
    with pytest.raises(SkillError, match="overwrite"):
        await copy(ctx, {"source": "a.txt", "destination": "docs"})
    with pytest.raises(SkillError, match="secrets"):
        await copy(ctx, {"source": ".ssh/id_rsa", "destination": "docs"})


@pytest.mark.asyncio
async def test_open_settings_pane_and_unknown(tmp_path):
    from jarvis_core.errors import SkillError

    ctx = _ctx(tmp_path)
    run = build_registry().get("open_settings").handler
    assert "bluetooth" in await run(ctx, {"pane": "bluetooth"})
    assert ctx.runner.ran("open", "x-apple.systempreferences:com.apple.BluetoothSettings")
    with pytest.raises(SkillError):
        await run(ctx, {"pane": "flux capacitor"})
    m = IntentRouter.load().match("open bluetooth settings")
    assert m and m.tool == "open_settings" and m.args["pane"] == "bluetooth"


def test_dismiss_phrases():
    from jarvis_core.daemon import Daemon
    for t in ["close jarvis", "Jarvis close", "jarvis, close the chat", "hide the conversation", "stop", "never mind",
              "that's all", "go away", "dismiss", "close jarvis please", "jarvis stop talking"]:
        assert Daemon.is_dismiss(t), t
    for t in ["close this tab", "close all tabs", "stop the music", "open jarvis", "what is the jarvis", "close safari",
              "quit all apps"]:
        assert not Daemon.is_dismiss(t), t
