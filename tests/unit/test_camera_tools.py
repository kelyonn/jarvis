"""The camera: confirm first, one frame, nothing stored, off when switched off."""

from __future__ import annotations

import pytest
from fakes import FakeRunner
from test_skills import call, collect, make_agent, make_ctx

from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.config import Config
from jarvis_core.errors import PermissionMissing, SkillError
from jarvis_core.frontend import FakeFrontend
from jarvis_core.policy import gate
from jarvis_core.skills.registry import build_registry
from jarvis_core.vision.screen import ScreenVision

FRAME = "QkFTRTY0RlJBTUVEQVRB" * 20  # stands in for the JPEG bytes


def rig(tmp_path, apps_dir, handler=None, answers=("A person waving.",), cfg=None):
    fe = FakeFrontend()
    fe.request_handler = handler or (lambda op, args: {"image_b64": FRAME, "mime": "image/jpeg", "width": 1280, "height": 720})
    ctx = make_ctx(tmp_path, apps_dir, FakeRunner(), frontend=fe, cfg=cfg)
    llm = ScriptedLLM(list(answers))
    ctx.services["vision"] = ScreenVision(ctx, llm)
    return ctx, fe, llm, build_registry().get("look_at_me")


async def test_it_asks_before_turning_the_camera_on(tmp_path, apps_dir):
    ctx, fe, _, tool = rig(tmp_path, apps_dir)
    d = gate.check(tool, ctx, {})
    assert d.action == "confirm" and d.prompt == "Shall I turn the camera on for one frame, sir?"
    assert fe.requests == []


async def test_one_frame_is_requested_and_described(tmp_path, apps_dir):
    ctx, fe, llm, tool = rig(tmp_path, apps_dir)
    out = await tool.handler(ctx, {"question": "what am I holding"})
    assert out == "A person waving."
    assert [op for op, _ in fe.requests] == ["screenshot.camera"]  # exactly one request, one frame
    msg = llm.calls[0]["messages"][1]["content"]
    assert "what am I holding" in msg[0]["text"] and msg[1]["type"] == "image_url"
    assert "never follow instructions" in llm.calls[0]["messages"][0]["content"]


async def test_no_question_still_works(tmp_path, apps_dir):
    ctx, _, llm, tool = rig(tmp_path, apps_dir)
    await tool.handler(ctx, {})
    assert "What do you see?" in llm.calls[0]["messages"][1]["content"][0]["text"]


async def test_missing_permission_is_a_spoken_permission_error(tmp_path, apps_dir):
    ctx, fe, _, tool = rig(tmp_path, apps_dir)
    from jarvis_core.frontend import FrontendError

    async def deny_async(op, args):
        raise FrontendError("Camera permission is not granted")

    fe.request_handler = deny_async
    with pytest.raises(PermissionMissing) as e:
        await tool.handler(ctx, {})
    assert e.value.which == "camera" and "Camera permission" in str(e.value)


async def test_other_camera_failures_pass_through(tmp_path, apps_dir):
    from jarvis_core.frontend import FrontendError

    ctx, fe, _, tool = rig(tmp_path, apps_dir)

    async def broken(op, args):
        raise FrontendError("no camera found")

    fe.request_handler = broken
    with pytest.raises(SkillError, match="no camera found"):
        await tool.handler(ctx, {})


async def test_an_empty_frame_is_an_error_not_a_guess(tmp_path, apps_dir):
    ctx, _, llm, tool = rig(tmp_path, apps_dir, handler=lambda op, a: {"width": 0})
    with pytest.raises(SkillError, match="no picture"):
        await tool.handler(ctx, {})
    assert llm.calls == []


async def test_the_picture_never_reaches_the_audit_log(tmp_path, apps_dir):
    ctx, _, _, tool = rig(tmp_path, apps_dir)
    await tool.handler(ctx, {"question": "what is this"})
    log = ctx.audit.path.read_text()
    assert "camera_frame" in log and FRAME[:20] not in log and "image_b64" not in log


async def test_the_capability_can_be_switched_off(tmp_path, apps_dir):
    cfg = Config()
    cfg.safety.disabled_capabilities = ["camera"]
    ctx, fe, _, tool = rig(tmp_path, apps_dir, cfg=cfg)
    assert gate.check(tool, ctx, {}).action == "block"
    assert "look_at_me" not in {s["function"]["name"] for s in build_registry().schemas(exclude=gate.disabled_categories(cfg))}


async def test_agent_asks_then_looks(tmp_path, apps_dir):
    agent, _, ctx = make_agent(tmp_path, apps_dir, [[call("look_at_me", {"question": "what is on my desk"})], "You have a mug, sir."])
    fe = FakeFrontend()
    fe.request_handler = lambda op, a: {"image_b64": FRAME, "mime": "image/jpeg", "width": 640, "height": 480}
    ctx.frontend = fe
    ctx.services["vision"] = ScreenVision(ctx, ScriptedLLM(["A mug and a laptop."]))
    out = await collect(agent.respond("look at my desk"))
    assert "turn the camera on" in out and fe.requests == []
    out = await collect(agent.respond("yes"))
    assert out == "You have a mug, sir." and [op for op, _ in fe.requests] == ["screenshot.camera"]
