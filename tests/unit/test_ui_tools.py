"""Controlling any app through its accessibility tree: references, per-app rules, honest verification."""

from __future__ import annotations

import copy

import pytest
from fakes import FakeRunner
from test_skills import call, collect, make_agent, make_ctx

from jarvis_core.config import Config
from jarvis_core.errors import SkillError
from jarvis_core.frontend import FakeFrontend
from jarvis_core.policy import gate
from jarvis_core.skills import ui_tools as U
from jarvis_core.skills.registry import build_registry


def node(id_, role, title="", value=None, actions=None, children=None, frame=(10, 10, 80, 20)):
    n = {"id": id_, "role": role, "frame": list(frame)}
    if title:
        n["title"] = title
    if value is not None:
        n["value"] = value
    if actions:
        n["actions"] = actions
    if children:
        n["children"] = children
    return n


def notes_tree():
    return node("e1", "AXWindow", "Untitled", children=[
        node("e2", "AXButton", "Save", actions=["Press"]),
        node("e3", "AXButton", "Send", actions=["Press"]),
        node("e4", "AXTextField", "Search", value="", actions=[]),
        node("e5", "AXCheckBox", "Pin note", value="0", actions=["Press"]),
        node("e6", "AXStaticText", value="Shopping list"),
        node("e7", "AXGroup", children=[node("e8", "AXGroup")]),
        node("e9", "AXSecureTextField", "Password", value="•••"),
        node("e10", "AXButton", "Hidden", actions=["Press"], frame=(0, 0, 0, 0)),
    ])


class Screen:
    """A fake Mac: serves a tree, applies actions to it, and records what the Shell was asked."""

    def __init__(self, app="Notes", tree=None, on_press=None):
        self.app, self.tree, self.on_press = app, tree or notes_tree(), on_press
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, op, args):
        self.calls.append((op, args))
        if op == "ax.tree":
            return {"app": self.app, "tree": copy.deepcopy(self.tree), "count": 9}
        if op == "ax.press" and self.on_press:
            self.on_press(self.tree, args)
        if op == "ax.set_value":
            for n in U.walk(self.tree):
                if n["id"] == args["element_id"]:
                    n["value"] = args["text"]
        return {"ok": True}


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(U, "SETTLE_S", 0)


def rig(tmp_path, apps_dir, screen, cfg=None):
    fe = FakeFrontend()
    fe.request_handler = screen
    ctx = make_ctx(tmp_path, apps_dir, FakeRunner(), frontend=fe, cfg=cfg)
    reg = build_registry()
    return ctx, reg.get("ui_look"), reg.get("ui_do")


# ------------------------------------------------------------------ looking


async def test_look_lists_what_can_be_touched_with_references(tmp_path, apps_dir):
    ctx, look, _ = rig(tmp_path, apps_dir, Screen())
    out = await look.handler(ctx, {})
    assert out.startswith("Notes (snapshot 1,")
    assert 's1:e2 button "Save"' in out and 's1:e4 field "Search"' in out
    assert 's1:e5 checkbox "Pin note" = "off"' in out
    assert "password field" in out or "e9" not in out  # secure fields are never shown with a value
    assert "••" not in out and "Hidden" not in out and "e7" not in out  # no empty groups, no zero-size items
    assert 'text "Shopping list"' in out


async def test_look_says_so_when_an_app_exposes_nothing(tmp_path, apps_dir):
    ctx, look, _ = rig(tmp_path, apps_dir, Screen(tree=node("e1", "AXWindow")))
    assert "nothing I can interact with" in await look.handler(ctx, {})


async def test_look_passes_the_app_name_through(tmp_path, apps_dir):
    screen = Screen()
    ctx, look, _ = rig(tmp_path, apps_dir, screen)
    await look.handler(ctx, {"app": "Notes"})
    assert screen.calls[0][1]["app"] == "Notes"


# ------------------------------------------------------------------ references expire


async def test_a_reference_from_an_older_look_is_refused(tmp_path, apps_dir):
    screen = Screen()
    ctx, look, do = rig(tmp_path, apps_dir, screen)
    await look.handler(ctx, {})
    await look.handler(ctx, {})  # a second look: snapshot 2
    with pytest.raises(SkillError, match="moved on"):
        await do.handler(ctx, {"ref": "s1:e2", "action": "press"})
    assert not any(op == "ax.press" for op, _ in screen.calls)


async def test_garbage_and_unknown_references_are_refused(tmp_path, apps_dir):
    ctx, look, do = rig(tmp_path, apps_dir, Screen())
    await look.handler(ctx, {})
    with pytest.raises(SkillError, match="isn't a reference"):
        await do.handler(ctx, {"ref": "e2", "action": "press"})
    with pytest.raises(SkillError, match="isn't in the current look"):
        await do.handler(ctx, {"ref": "s1:e999", "action": "press"})


async def test_no_look_at_all_means_no_action(tmp_path, apps_dir):
    ctx, _, do = rig(tmp_path, apps_dir, Screen())
    with pytest.raises(SkillError, match="moved on"):
        await do.handler(ctx, {"ref": "s1:e2", "action": "press"})


async def test_actions_expire_their_own_references(tmp_path, apps_dir):
    ctx, look, do = rig(tmp_path, apps_dir, Screen())
    await look.handler(ctx, {})
    await do.handler(ctx, {"ref": "s1:e2", "action": "press"})
    with pytest.raises(SkillError, match="moved on"):
        await do.handler(ctx, {"ref": "s1:e2", "action": "press"})


# ------------------------------------------------------------------ acting and checking


async def test_press_reports_what_changed(tmp_path, apps_dir):
    def open_dialog(tree, args):
        tree["children"].append(node("e20", "AXButton", "Cancel", actions=["Press"]))

    ctx, look, do = rig(tmp_path, apps_dir, Screen(on_press=open_dialog))
    await look.handler(ctx, {})
    out = await do.handler(ctx, {"ref": "s1:e2", "action": "press"})
    assert out.startswith("Pressed Save in Notes.") and "now showing 'Cancel'" in out and "expired" in out


async def test_press_that_does_nothing_says_nothing_changed(tmp_path, apps_dir):
    ctx, look, do = rig(tmp_path, apps_dir, Screen())
    await look.handler(ctx, {})
    out = await do.handler(ctx, {"ref": "s1:e5", "action": "press"})
    assert "Nothing on screen visibly changed" in out  # it does not pretend the checkbox flipped


async def test_set_value_is_confirmed_by_reading_it_back(tmp_path, apps_dir):
    screen = Screen()
    ctx, look, do = rig(tmp_path, apps_dir, screen)
    await look.handler(ctx, {})
    out = await do.handler(ctx, {"ref": "s1:e4", "action": "set_value", "text": "milk"})
    assert "now holds that text" in out
    assert ("ax.set_value", {"element_id": "e4", "text": "milk"}) in screen.calls


async def test_set_value_that_does_not_stick_is_reported(tmp_path, apps_dir):
    screen = Screen()
    orig = screen.__call__

    def ignore_set(op, args):
        return {"ok": True} if op == "ax.set_value" else orig(op, args)

    ctx, look, do = rig(tmp_path, apps_dir, screen)
    ctx.frontend.request_handler = ignore_set
    await look.handler(ctx, {})
    out = await do.handler(ctx, {"ref": "s1:e4", "action": "set_value", "text": "milk"})
    assert "couldn't confirm" in out


async def test_set_value_only_on_text_fields(tmp_path, apps_dir):
    ctx, look, do = rig(tmp_path, apps_dir, Screen())
    await look.handler(ctx, {})
    with pytest.raises(SkillError, match="isn't a text field"):
        await do.handler(ctx, {"ref": "s1:e2", "action": "set_value", "text": "x"})


async def test_click_uses_the_centre_of_the_element(tmp_path, apps_dir):
    screen = Screen()
    ctx, look, do = rig(tmp_path, apps_dir, screen)
    await look.handler(ctx, {})
    await do.handler(ctx, {"ref": "s1:e2", "action": "click"})
    assert ("input.click", {"x": 50.0, "y": 20.0, "label": "Save"}) in screen.calls


# ------------------------------------------------------------------ rules


def decide(ctx, do, ref, action="press", text=""):
    return gate.check(do, ctx, {"ref": ref, "action": action, "text": text})


async def test_dangerous_controls_are_always_refused(tmp_path, apps_dir):
    ctx, look, do = rig(tmp_path, apps_dir, Screen())
    await look.handler(ctx, {})
    d = decide(ctx, do, "s1:e3")  # "Send"
    assert d.action == "block" and "Send" in d.reason
    assert decide(ctx, do, "s1:e2").action == "allow"  # "Save"


@pytest.mark.parametrize("app,expected", [
    ("Terminal", "never"), ("iTerm2", "never"), ("1Password", "never"), ("Slack", "ask"), ("Messages", "ask"),
    ("Notes", "auto"), ("Safari.app", "auto"),
])
def test_built_in_app_rules(app, expected):
    assert U.app_rule(Config(), app) == expected


def test_user_rules_override_the_built_in_ones():
    cfg = Config()
    cfg.safety.app_rules = {"terminal": "auto", "Notes": "ask", "Odd": "banana"}
    assert U.app_rule(cfg, "Terminal") == "auto" and U.app_rule(cfg, "notes") == "ask"
    assert U.app_rule(cfg, "Odd") == "ask"  # a rule it doesn't understand errs on the side of asking


async def test_never_apps_can_be_looked_at_but_not_touched(tmp_path, apps_dir):
    ctx, look, do = rig(tmp_path, apps_dir, Screen(app="Terminal"))
    out = await look.handler(ctx, {})
    assert "look only" in out
    assert decide(ctx, do, "s1:e2").action == "block"


async def test_ask_apps_need_a_spoken_yes_with_a_clear_preview(tmp_path, apps_dir):
    ctx, look, do = rig(tmp_path, apps_dir, Screen(app="Slack"))
    await look.handler(ctx, {})
    d = decide(ctx, do, "s1:e4", "set_value", "hello team")
    assert d.action == "confirm" and d.prompt == "Shall I put 'hello team' into Search in Slack?"
    assert decide(ctx, do, "s1:e2").prompt == "Shall I press Save in Slack?"


async def test_screen_capability_switch_turns_the_ui_tools_off(tmp_path, apps_dir):
    cfg = Config()
    cfg.safety.disabled_capabilities = ["screen"]
    ctx, _, do = rig(tmp_path, apps_dir, Screen(), cfg=cfg)
    assert gate.check(do, ctx, {"ref": "s1:e2"}).action == "block"
    names = {s["function"]["name"] for s in build_registry().schemas(exclude=gate.disabled_categories(cfg))}
    assert "ui_look" not in names and "ui_do" not in names


async def test_agent_round_trip_look_then_act(tmp_path, apps_dir):
    screen = Screen()
    agent, _llm, ctx = make_agent(tmp_path, apps_dir, [
        [call("ui_look", {})],
        [call("ui_do", {"ref": "s1:e2", "action": "press"}, "c2")],
        "That is done, sir.",
    ])
    fe = FakeFrontend()
    fe.request_handler = screen
    ctx.frontend = fe
    out = await collect(agent.respond("press the save button"))
    assert out == "That is done, sir."
    assert [op for op, _ in screen.calls].count("ax.press") == 1
