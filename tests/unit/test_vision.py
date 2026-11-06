from __future__ import annotations

import asyncio
import json

import pytest
from fakes import FakeRunner, Rig

from jarvis_core import runtime as rtmod
from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.config import Config
from jarvis_core.errors import LLMUnavailable, SkillError
from jarvis_core.frontend import FrontendError
from jarvis_core.policy.audit import AuditLog
from jarvis_core.skills import vision_tools as VT
from jarvis_core.skills.base import CmdResult, Risk, ToolContext, ToolRegistry
from jarvis_core.state import State
from jarvis_core.vision.computer_use import ComputerUse, parse_action
from jarvis_core.vision.screen import ScreenVision, is_private_app

TREE = {"id": "w", "role": "AXWindow", "children": [
    {"id": "e1", "role": "AXButton", "title": "Search", "actions": ["Press"], "frame": [100, 100, 80, 30]},
    {"id": "e2", "role": "AXTextField", "desc": "To", "frame": [100, 150, 300, 30]},
    {"id": "e3", "role": "AXButton", "title": "Send", "actions": ["Press"], "frame": [500, 600, 60, 30]},
    {"id": "e4", "role": "AXButton", "title": "Attach file", "actions": ["Press"], "frame": [420, 600, 60, 30]},
    {"id": "e5", "role": "AXLink", "title": "Delete account", "frame": [10, 10, 100, 20]},
    {"id": "e6", "role": "AXStaticText", "title": "ignored text", "frame": [0, 0, 50, 10]},
]}


class FakeShell:
    connected = True

    def __init__(self, app="Mail", press_error=None):
        self.requests: list[tuple[str, dict]] = []
        self.app, self.press_error = app, press_error

    async def request(self, op, args, timeout=10.0):
        self.requests.append((op, dict(args)))
        if op == "ax.frontmost":
            return {"app": self.app, "window": "Inbox"}
        if op == "screenshot":
            return {"image_b64": "AAAA", "mime": "image/jpeg", "width": 800, "height": 500, "scale_x": 2.0, "scale_y": 2.0}
        if op == "ax.tree":
            return {"tree": TREE}
        if op == "ax.press" and self.press_error:
            raise FrontendError(self.press_error)
        return {"ok": True}

    def ops(self, name):
        return [a for o, a in self.requests if o == name]


def act(action, say="", **kw):
    return json.dumps({"say": say, "action": action, **kw})


def make(replies, shell=None, runner=None, **kw):
    shell = shell or FakeShell()
    narrated: list[str] = []

    async def narrate(text):
        narrated.append(text)

    ctx = ToolContext(cfg=Config(), frontend=shell, runner=runner or FakeRunner(), audit=AuditLog(), narrate=narrate)
    llm = ScriptedLLM(replies)
    cu = ComputerUse(ctx, llm, pause_s=0, **kw)
    return cu, ctx, shell, llm, narrated


def assert_never_sent(shell):
    for a in shell.ops("ax.press"):
        assert a["label"].lower() not in ("send", "delete account")
    for a in shell.ops("input.click"):
        assert "send" not in a["label"].lower() and "delete" not in a["label"].lower()
    for a in shell.ops("input.key"):
        assert a["key"].lower() not in ("return", "enter")
    for a in shell.ops("input.type"):
        assert "\n" not in a["text"]


# ----------------------------------------------------------------------------- screen vision


async def test_describe_builds_image_message_with_vision_role():
    shell, llm = FakeShell(app="Terminal"), ScriptedLLM(["A Python traceback about a missing module."])
    ctx = ToolContext(cfg=Config(), frontend=shell, audit=AuditLog())
    out = await ScreenVision(ctx, llm).describe("explain this error")
    assert out == "A Python traceback about a missing module."
    call = llm.calls[0]
    assert call["role"] == "vision" and call["tools"] is None
    user = call["messages"][1]["content"]
    assert user[0]["type"] == "text" and "Front app: Terminal" in user[0]["text"] and "explain this error" in user[0]["text"]
    assert user[1]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"
    assert "untrusted" in call["messages"][0]["content"]
    assert shell.ops("screenshot")[0]["target"] == "window"


async def test_screenshot_refused_for_password_managers():
    assert is_private_app("1Password 7") and not is_private_app("Safari")
    shell = FakeShell(app="1Password")
    ctx = ToolContext(cfg=Config(), frontend=shell)
    with pytest.raises(SkillError, match="won't look at 1Password"):
        await ScreenVision(ctx, ScriptedLLM([])).describe("what is this")
    assert shell.ops("screenshot") == []


async def test_describe_error_mapping_and_no_llm():
    class Down:
        async def complete(self, *a, **k):
            raise LLMUnavailable("down")

    ctx = ToolContext(cfg=Config(), frontend=FakeShell(), audit=AuditLog())
    with pytest.raises(SkillError, match="couldn't reach my vision model"):
        await ScreenVision(ctx, Down()).describe("x")
    with pytest.raises(SkillError, match="without my brain"):
        await ScreenVision(ctx, None).describe("x")
    ctx2 = ToolContext(cfg=Config(), frontend=None)
    with pytest.raises(SkillError, match="shell app"):
        await ScreenVision(ctx2, ScriptedLLM([])).describe("x")


# ------------------------------------------------------------------------------ action parse


def test_parse_action():
    assert parse_action('Sure! {"action":"wait","seconds":1} thanks')["action"] == "wait"
    assert parse_action('```json\n{"action": "done", "summary": "ok"}\n```')["summary"] == "ok"
    for bad in ("no json here", '{"action":"format_disk"}', '{"say":"hi"}', "[1,2]"):
        with pytest.raises(ValueError):
            parse_action(bad)


# ------------------------------------------------------------------------------ the loop


async def test_happy_path_is_narrated_and_bracketed_by_input_session():
    cu, ctx, shell, llm, said = make([
        act("open_app", "Opening Mail", app="Mail"),
        act("click", "Pressing search", element="e1"),
        act("type", "Typing the name", text="Priya"),
        act("done", summary="I've searched for Priya, sir."),
    ])
    res = await cu.run("search Mail for Priya")
    assert res.done and res.steps == 4 and res.spoken() == "I've searched for Priya, sir."
    assert said == ["Opening Mail", "Pressing search", "Typing the name"]
    assert ctx.runner.ran("open", "-a", "Mail")
    assert shell.ops("ax.press") == [{"element_id": "e1", "label": "Search"}]
    assert shell.ops("input.type") == [{"text": "Priya"}]
    names = [o for o, _ in shell.requests]
    assert names[0] == "input.begin" and names[-1] == "input.end"
    assert llm.calls[0]["role"] == "vision" and llm.calls[0]["tools"] is None
    prompt = llm.calls[0]["messages"][1]["content"][0]["text"]
    assert 'e1 Button "Search"' in prompt and "ignored text" not in prompt and "Goal: search Mail for Priya" in prompt
    assert not cu.running


async def test_send_delete_and_return_are_blocked_then_the_model_adapts():
    cu, ctx, shell, llm, _ = make([
        act("click", element="e3"),  # Send
        act("key", key="return"),
        act("type", text="line one\nline two"),
        act("done", summary="I stopped before sending, sir."),
    ])
    res = await cu.run("email Priya and send it")
    assert res.stopped == "blocked" and not res.done  # three refusals in a row end the run
    assert_never_sent(shell)
    history = llm.calls[-1]["messages"][1]["content"][0]["text"]
    assert "BLOCKED" in history


async def test_blocked_actions_do_not_end_the_run_if_the_model_adapts():
    cu, ctx, shell, llm, _ = make([
        act("click", element="e3"),
        act("click", element="e4"),
        act("done", summary="Attached, sir. The send is yours."),
    ])
    res = await cu.run("attach the file")
    assert res.done and shell.ops("ax.press") == [{"element_id": "e4", "label": "Attach file"}]
    assert_never_sent(shell)


async def test_coordinate_clicks_cannot_dodge_the_filter():
    # (260, 310) in screenshot px = (520, 620) in screen points = inside the Send button
    cu, ctx, shell, llm, _ = make([
        act("click", x=260, y=310, label="the green button"),
        act("click", x=40, y=40, label="blue link"),  # (80, 80): nothing there, fine
        act("done", summary="ok"),
    ])
    res = await cu.run("click things")
    assert res.done
    clicks = shell.ops("input.click")
    assert len(clicks) == 1 and clicks[0]["x"] == 80 and clicks[0]["y"] == 80
    assert_never_sent(shell)


async def test_chords_that_send_or_quit_are_blocked():
    cu, ctx, shell, llm, _ = make([
        act("key", key="return", modifiers=["cmd"]),
        act("key", key="q", modifiers=["cmd"]),
        act("key", key="v", modifiers=["cmd"]),
        act("done", summary="pasted"),
    ])
    res = await cu.run("paste")
    assert res.done and shell.ops("input.key") == [{"key": "v", "modifiers": ["cmd"]}]


async def test_shell_refusal_counts_as_blocked_not_error():
    shell = FakeShell(press_error="refusing to press 'Attach file': send/post/buy/delete style controls are blocked")
    cu, ctx, _, llm, _ = make([act("click", element="e4"), act("done", summary="ok")], shell=shell)
    res = await cu.run("attach")
    assert res.done
    assert "BLOCKED" in llm.calls[1]["messages"][1]["content"][0]["text"]


async def test_kill_switch_stops_before_the_next_action():
    cu, ctx, shell, llm, said = make([act("open_app", "Opening Mail", app="Mail"), act("open_app", app="Safari")])

    async def narrate(text):
        cu.request_stop()  # the user says "stop" while Jarvis is talking

    ctx.narrate = narrate
    res = await cu.run("open things")
    assert res.stopped == "killed" and res.spoken().startswith("Stopped, sir.")
    assert ctx.runner.calls == []  # not even the first app was opened
    assert [o for o, _ in shell.requests][-1] == "input.end"
    cu.running = True
    await cu.on_cancel()
    assert cu.stop_requested


async def test_cancel_cleans_up():
    flags = []
    cu, ctx, shell, llm, _ = make([], set_active=flags.append)

    async def slow(*a, **k):
        await asyncio.sleep(30)

    llm.complete = slow
    task = asyncio.create_task(cu.run("anything"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert flags == [True, False] and not cu.running
    assert [o for o, _ in shell.requests][-1] == "input.end"


async def test_budget_repeats_and_garbage():
    cu, *_ = make([act("scroll", dy=-i) for i in range(1, 10)], max_steps=3)
    res = await cu.run("scroll forever")
    assert res.stopped == "budget" and res.steps == 3

    cu, *_ = make([act("scroll", dy=-3)] * 5)
    res = await cu.run("scroll")
    assert res.stopped == "stuck" and "circles" in res.summary

    cu, *_ = make(["I think I should click", "nope", "still no json"])
    res = await cu.run("anything")
    assert res.stopped == "stuck"

    clock = iter([0, 0, 0, 500, 500, 500, 500, 500])
    cu, *_ = make([act("wait", seconds=0.2)] * 3, clock=lambda: next(clock))
    assert (await cu.run("wait")).stopped == "budget"


async def test_errors_end_the_run_and_private_apps_are_refused():
    cu, ctx, shell, llm, _ = make([act("open_app", app="Nonexistent")] * 1 + [act("open_app", app="Nope2"), act("open_app", app="Nope3")])
    ctx.runner = FakeRunner({("open",): CmdResult(1, "", "not found")})
    res = await cu.run("open stuff")
    assert res.stopped == "stuck"

    cu, ctx, shell, llm, _ = make([], shell=FakeShell(app="1Password"))
    res = await cu.run("look around")
    assert res.stopped == "blocked" and llm.calls == []

    cu, ctx, shell, llm, _ = make([act("open_app", app="1Password"), act("done", summary="x")])
    assert (await cu.run("open my vault")).done  # the open was refused (blocked), the model gave up gracefully
    assert not ctx.runner.ran("open", "-a", "1Password")

    cu, ctx, shell, llm, _ = make([act("open_url", url="javascript:alert(1)"), act("done", summary="x")])
    await cu.run("go")
    assert not any(c[0] == "open" for c in ctx.runner.calls)


async def test_requires_llm_and_audits():
    ctx = ToolContext(cfg=Config(), frontend=FakeShell(), audit=AuditLog())
    with pytest.raises(SkillError):
        await ComputerUse(ctx, None).run("x")
    cu, ctx, shell, llm, _ = make([act("done", summary="all good")])
    await cu.run("do a thing")
    kinds = [json.loads(line)["kind"] for line in ctx.audit.path.read_text().splitlines()]
    assert kinds[0] == "cu_start" and kinds[-1] == "cu_end"


# ---------------------------------------------------------------------------------- tools


async def test_tools_confirm_and_run():
    reg = ToolRegistry()
    VT.register(reg)
    cu_tool = reg.get("computer_use")
    assert cu_tool.risk == Risk.CONFIRM
    prompt = cu_tool.decide(ToolContext(cfg=Config()), {"goal": "open my calendar."}).prompt
    assert "open my calendar" in prompt and "say stop" in prompt and prompt.endswith("Shall I proceed?")

    cu, ctx, shell, llm, _ = make([act("done", summary="Calendar is open, sir.")])
    ctx.services.update(computer_use=cu, vision=ScreenVision(ctx, ScriptedLLM(["A calendar app."])))
    assert await cu_tool.handler(ctx, {"goal": "open my calendar"}) == "Calendar is open, sir."
    assert await reg.get("describe_screen").handler(ctx, {"question": "what is this"}) == "A calendar app."
    cu.running = True
    with pytest.raises(SkillError, match="already in the middle"):
        await cu_tool.handler(ctx, {"goal": "again"})
    with pytest.raises(SkillError, match="isn't available"):
        await reg.get("describe_screen").handler(ToolContext(cfg=Config()), {})


# ----------------------------------------------------------------------------- daemon wiring


@pytest.fixture
async def wired(monkeypatch):
    rig = Rig()
    rt = rtmod.Runtime(rig.cfg, rig.daemon, rig.frontend, rig.assistant)
    rig.assistant.ctx.narrate = rig.daemon.narrate
    rtmod._attach_vision(rt, rig.llm)
    await rig.start()
    yield rig, rt
    await rig.stop()


async def test_kill_paths_reach_the_computer_use_loop(wired):
    rig, rt = wired
    cu = rt.extras["computer_use"]
    cu.running = True
    rig.daemon.on_orb_click("kill")  # the shell's mouse-move kill switch arrives as this
    for _ in range(100):
        if cu.stop_requested:
            break
        await asyncio.sleep(0.01)
    assert cu.stop_requested
    cu.running = False
    cu.stop_requested = False
    await rig.daemon.cancel_everything()  # hotkey long-press
    assert not cu.stop_requested  # nothing running, so nothing to stop


async def test_narrate_speaks_without_changing_state_and_only_mid_turn(wired):
    rig, rt = wired
    await rig.daemon.narrate("ignored while idle")
    assert rig.tts.spoken == []
    rig.daemon.sm.go(State.THINKING)
    await rig.daemon.narrate("Opening Mail")
    assert rig.tts.spoken == ["Opening Mail"] and rig.daemon.state == State.THINKING
    rig.daemon.sm.go(State.IDLE)
    assert rt.assistant.ctx.services["computer_use"].running is False
    assert rig.daemon.cu_active is False
