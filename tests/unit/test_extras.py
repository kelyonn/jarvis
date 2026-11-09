from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fakes import FakeRunner

from jarvis_core.config import Config
from jarvis_core.errors import SkillError
from jarvis_core.policy.audit import AuditLog
from jarvis_core.router import IntentRouter
from jarvis_core.skills.base import CmdResult, Risk, ToolContext
from jarvis_core.skills.registry import build_registry


def ctx_for(tmp_path, runner) -> ToolContext:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return ToolContext(
        cfg=Config(), runner=runner, audit=AuditLog(tmp_path / "audit.log"), app_dirs=[], home_dir=home,
        clock=lambda: datetime(2026, 11, 4, 12, 0).astimezone(),
    )


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_new_tools_registered_with_expected_risk():
    reg = build_registry()
    for name in ["take_screenshot", "set_focus", "create_calendar_event", "close_tab", "search_messages"]:
        assert reg.get(name).risk == Risk.SAFE
    for name in ["delete_event", "delete_reminder", "close_all_tabs", "quit_all_apps"]:
        assert reg.get(name).risk == Risk.CONFIRM


def _shot_ctx(tmp_path, handler=None):
    import base64

    from jarvis_core.frontend import FakeFrontend

    ctx = ctx_for(tmp_path, FakeRunner())
    fe = FakeFrontend()
    fe.request_handler = handler or (lambda op, args: {"image_b64": base64.b64encode(b"jpegbytes").decode()})
    ctx.frontend = fe
    return ctx, fe


def test_screenshot_is_taken_by_the_shell_and_saved(tmp_path):
    ctx, fe = _shot_ctx(tmp_path)
    out = run(build_registry().get("take_screenshot").handler(ctx, {"target": "window"}))
    op, args = fe.requests[-1]
    assert op == "screenshot" and args["target"] == "window"
    files = list((tmp_path / "home" / "Desktop" / "Screenshots").glob("*.jpg"))
    assert len(files) == 1 and files[0].read_bytes() == b"jpegbytes"
    assert "front window" in out and files[0].name in out


def test_screenshot_modes_reach_the_shell(tmp_path):
    ctx, fe = _shot_ctx(tmp_path)
    tool = build_registry().get("take_screenshot")
    run(tool.handler(ctx, {"target": "selection"}))
    assert fe.requests[-1][1]["target"] == "selection"
    run(tool.handler(ctx, {"target": "display 2", "include_cursor": True}))
    assert fe.requests[-1][1]["target"] == "display:2" and fe.requests[-1][1]["include_cursor"] is True


def test_screenshot_to_clipboard_and_empty_result(tmp_path):
    ctx, _fe = _shot_ctx(tmp_path)
    out = run(build_registry().get("take_screenshot").handler(ctx, {"target": "screen", "to_clipboard": True}))
    assert "clipboard" in out and ctx.runner.ran("osascript")
    ctx2, _ = _shot_ctx(tmp_path, lambda op, args: {"image_b64": ""})
    with pytest.raises(SkillError, match="came back empty"):
        run(build_registry().get("take_screenshot").handler(ctx2, {"target": "screen"}))


def test_focus_runs_shortcut_and_reports_missing(tmp_path):
    have = CmdResult(0, "Set Focus Work\nTurn Off Focus")
    runner = FakeRunner({("shortcuts", "list"): have})
    ctx = ctx_for(tmp_path, runner)
    tool = build_registry().get("set_focus")
    run(tool.handler(ctx, {"name": "work", "state": "on"}))
    assert runner.calls[-1] == ["shortcuts", "run", "Set Focus Work"]
    run(tool.handler(ctx, {"state": "off"}))
    assert runner.calls[-1] == ["shortcuts", "run", "Turn Off Focus"]
    with pytest.raises(SkillError, match="isn't set up"):
        run(tool.handler(ctx, {"name": "dnd"}))


def test_create_event_passes_parsed_fields_as_argv(tmp_path):
    runner = FakeRunner()
    ctx = ctx_for(tmp_path, runner)
    out = run(build_registry().get("create_calendar_event").handler(
        ctx, {"title": "Dentist", "when": "tomorrow 4pm", "minutes": 45}))
    argv = runner.calls[-1]
    assert argv[0] == "osascript"
    tail = argv[3:]  # after -e <script>
    assert tail[0] == "Dentist" and tail[1:6] == ["2026", "11", "5", "16", "0"] and tail[6] == "45"
    assert "16:45" in out


def test_create_event_rejects_absurd_duration(tmp_path):
    ctx = ctx_for(tmp_path, FakeRunner())
    with pytest.raises(SkillError):
        run(build_registry().get("create_calendar_event").handler(ctx, {"title": "x", "when": "tomorrow 4pm", "minutes": 2}))


def test_delete_event_and_reminder_report_not_found(tmp_path):
    ctx = ctx_for(tmp_path, FakeRunner())  # empty osascript output = nothing matched
    reg = build_registry()
    with pytest.raises(SkillError):
        run(reg.get("delete_event").handler(ctx, {"title": "ghost"}))
    with pytest.raises(SkillError):
        run(reg.get("delete_reminder").handler(ctx, {"title": "ghost"}))
    ctx = ctx_for(tmp_path, FakeRunner({("osascript",): CmdResult(0, "Dentist")}))
    assert "Dentist" in run(reg.get("delete_event").handler(ctx, {"title": "dent"}))


def test_quit_all_apps_spares_finder_and_jarvis(tmp_path):
    runner = FakeRunner({("osascript", "-e", "tell application \"System Events\" to get name of every process whose background only is false"):
                         CmdResult(0, "Finder, Jarvis, Spotify, Safari")})
    ctx = ctx_for(tmp_path, runner)
    out = run(build_registry().get("quit_all_apps").handler(ctx, {}))
    quit_targets = [c[-1] for c in runner.calls if "to quit" in c[2]]
    assert sorted(quit_targets) == ["Safari", "Spotify"]
    assert "2" in out


def test_close_all_tabs_closes_front_window_only(tmp_path):
    runner = FakeRunner()
    ctx = ctx_for(tmp_path, runner)
    run(build_registry().get("close_all_tabs").handler(ctx, {"browser": "Safari"}))
    assert any("close front window" in c[2] for c in runner.calls if c[0] == "osascript")


@pytest.mark.parametrize("phrase,tool,args", [
    ("take a screenshot", "take_screenshot", {"target": "screen"}),
    ("screenshot this window", "take_screenshot", {"target": "window"}),
    ("screenshot a selected area", "take_screenshot", {"target": "selection"}),
    ("screenshot in 5 seconds", "take_screenshot", {"target": "screen", "delay_seconds": "5"}),
    ("take a screenshot to my clipboard", "take_screenshot", {"target": "screen", "to_clipboard": "true"}),
    ("turn on do not disturb", "set_focus", {"state": "on"}),
    ("turn off focus mode", "set_focus", {"state": "off"}),
])
def test_router_fast_paths(phrase, tool, args):
    router = IntentRouter.load()
    m = router.match(phrase)
    assert m is not None and m.tool == tool and m.args == args
