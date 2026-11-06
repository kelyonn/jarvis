from __future__ import annotations

import plistlib

import pytest
from fakes import FakeRunner
from test_skills import collect, make_agent

from jarvis_core.adapters import shortcuts as shortcuts_kit
from jarvis_core.errors import ShortcutMissing
from jarvis_core.interfaces import ToolCallEvent
from jarvis_core.skills.base import CmdResult


def test_every_default_shortcut_builds(tmp_path):
    for name in shortcuts_kit.DEFAULT_SET:
        wf = plistlib.loads(shortcuts_kit.build_file(name, tmp_path).read_bytes())
        assert wf["WFWorkflowActions"], name
    assert shortcuts_kit.can_build("set focus work") and not shortcuts_kit.can_build("Set Focus My Custom")


async def test_run_raises_missing_with_buildable_flag(tmp_path, apps_dir):
    _, _, ctx = make_agent(tmp_path, apps_dir, [], runner=FakeRunner({("shortcuts", "list"): CmdResult(0, "Other")}))
    with pytest.raises(ShortcutMissing) as e:
        await shortcuts_kit.run(ctx, "Turn Off Focus")
    assert e.value.buildable
    with pytest.raises(ShortcutMissing) as e:
        await shortcuts_kit.run(ctx, "Something Else")
    assert not e.value.buildable


async def test_agent_offers_to_make_missing_shortcut_and_builds_on_yes(tmp_path, apps_dir, monkeypatch):
    monkeypatch.setattr(shortcuts_kit, "shortcuts_dir", lambda: tmp_path)
    runner = FakeRunner({("shortcuts", "list"): CmdResult(0, "")})
    agent, _, ctx = make_agent(tmp_path, apps_dir, [[ToolCallEvent("c1", "set_focus", {"state": "off"})]], runner=runner)
    out = await collect(agent.respond("turn off do not disturb"))
    assert "'Turn Off Focus' shortcut isn't set up" in out and "make it" in out and agent.awaiting_confirmation
    out2 = await collect(agent.respond("yes"))
    assert "Add Shortcut" in out2 and not agent.awaiting_confirmation
    assert runner.ran("shortcuts", "sign") and runner.ran("open", str(tmp_path / "Turn Off Focus.shortcut"))


async def test_timer_runs_without_offering_a_shortcut(tmp_path, apps_dir):
    runner = FakeRunner({("shortcuts", "list"): CmdResult(0, "")})
    agent, _, ctx = make_agent(tmp_path, apps_dir, [[ToolCallEvent("c1", "timer_set", {"seconds": 15})], "Done, sir."], runner=runner)
    out = await collect(agent.respond("set a timer for 15 seconds"))
    assert "shortcut" not in out and not agent.awaiting_confirmation


async def test_switch_setting_runs_the_on_off_shortcut(tmp_path, apps_dir):
    from jarvis_core.skills.registry import build_registry

    runner = FakeRunner({("shortcuts", "list"): CmdResult(0, "Jarvis Night Shift On")})
    _, _, ctx = make_agent(tmp_path, apps_dir, [], runner=runner)
    tool = build_registry().get("switch_setting")
    assert await tool.handler(ctx, {"setting": "night shift", "state": "on"}) == "Night Shift is on."
    assert runner.calls[-1] == ["shortcuts", "run", "Jarvis Night Shift On"]
    with pytest.raises(ShortcutMissing) as e:
        await tool.handler(ctx, {"setting": "true tone", "state": "off"})
    assert e.value.buildable
