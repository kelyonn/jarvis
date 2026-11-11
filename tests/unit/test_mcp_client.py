"""MCP servers: config parsing, trust levels, and a real round trip against a tiny stdio server."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fakes import FakeRunner
from test_skills import call, collect, make_agent, make_ctx

from jarvis_core import mcp_client
from jarvis_core.config import Config
from jarvis_core.policy import gate
from jarvis_core.skills.base import Risk, ToolRegistry

SERVER = str(Path(__file__).parent.parent / "fixtures" / "demo_mcp_server.py")


def spec(**kw) -> mcp_client.ServerSpec:
    return mcp_client.ServerSpec(name="demo", command=sys.executable, args=[SERVER], **kw)


# ------------------------------------------------------------------ config


def test_missing_file_means_no_servers(tmp_path):
    assert mcp_client.load_specs(tmp_path / "nope.json") == []


def test_broken_file_is_ignored(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text("{not json")
    assert mcp_client.load_specs(p) == []


def test_specs_are_parsed_and_bad_entries_skipped(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"servers": {
        "My Server!": {"command": "npx", "args": ["x", 2], "env": {"K": "v"}, "trust": "ALLOW", "block": ["rm"]},
        "nocmd": {"args": []},
        "weird": {"command": "x", "trust": "banana"},
        "off": {"command": "x", "enabled": False},
    }}))
    got = {s.name: s for s in mcp_client.load_specs(p)}
    assert set(got) == {"My_Server", "weird", "off"}
    s = got["My_Server"]
    assert s.args == ["x", "2"] and s.env == {"K": "v"} and s.trust == "allow" and s.block == {"rm"}
    assert got["weird"].trust == "ask" and got["off"].enabled is False


def test_tool_names_are_safe_and_short():
    n = mcp_client.tool_name("srv", "do-the thing/now")
    assert n == "mcp_srv_do_the_thing_now"
    assert len(mcp_client.tool_name("s" * 80, "t" * 80)) == 64


@pytest.mark.parametrize("trust,read_only,expected", [
    ("readonly", True, Risk.SAFE), ("readonly", False, Risk.CONFIRM),
    ("ask", True, Risk.CONFIRM), ("allow", False, Risk.SAFE),
])
def test_trust_levels(trust, read_only, expected):
    assert mcp_client.risk_for(mcp_client.ServerSpec("s", "c", trust=trust), "t", read_only) == expected


def test_blocked_tools_always_block():
    s = mcp_client.ServerSpec("s", "c", trust="allow", block={"t"})
    assert mcp_client.risk_for(s, "t", True) == Risk.BLOCKED


# ------------------------------------------------------------------ real server


async def test_round_trip_with_a_real_stdio_server(tmp_path, apps_dir):
    reg = ToolRegistry()
    mgr = mcp_client.MCPManager()
    await mgr.start(reg, [spec()])
    try:
        assert mgr.failed == {} and mgr.started == {"demo": 3}
        add, erase, boom = reg.get("mcp_demo_add"), reg.get("mcp_demo_erase"), reg.get("mcp_demo_boom")
        assert add.category == "mcp" and add.untrusted_output
        assert add.risk == Risk.SAFE and erase.risk == Risk.CONFIRM  # read-only hint decides

        ctx = make_ctx(tmp_path, apps_dir, FakeRunner())
        assert (await add.handler(ctx, {"a": 2, "b": 5})).strip() == "7"
        assert "erased old" in await erase.handler(ctx, {"target": "old"})
        with pytest.raises(Exception, match="nope|couldn't|error"):
            await boom.handler(ctx, {})
        assert erase.decide(ctx, {"target": "old"}).prompt.startswith("Shall I use demo to erase")
    finally:
        await mgr.stop()


async def test_a_server_that_cannot_start_is_reported_not_fatal():
    reg = ToolRegistry()
    mgr = mcp_client.MCPManager()
    await mgr.start(reg, [mcp_client.ServerSpec("ghost", "/no/such/program")])
    await mgr.stop()
    assert "ghost" in mgr.failed and len(reg) == 0


async def test_mcp_tools_go_through_the_same_gate_and_can_be_switched_off(tmp_path, apps_dir):
    reg = ToolRegistry()
    mgr = mcp_client.MCPManager()
    await mgr.start(reg, [spec(block={"boom"})])
    try:
        cfg = Config()
        ctx = make_ctx(tmp_path, apps_dir, FakeRunner(), cfg=cfg)
        assert gate.check(reg.get("mcp_demo_boom"), ctx, {}).action == "block"
        assert gate.check(reg.get("mcp_demo_erase"), ctx, {"target": "x"}).action == "confirm"
        cfg.safety.disabled_capabilities = ["mcp"]
        assert gate.check(reg.get("mcp_demo_add"), ctx, {"a": 1, "b": 1}).action == "block"
        assert not {s["function"]["name"] for s in reg.schemas(exclude=gate.disabled_categories(cfg))}
    finally:
        await mgr.stop()


async def test_agent_asks_before_running_a_risky_mcp_tool(tmp_path, apps_dir):
    mgr = mcp_client.MCPManager()
    agent, _llm, ctx = make_agent(tmp_path, apps_dir, [[call("mcp_demo_erase", {"target": "reports"})], "Done, sir."])
    await mgr.start(agent.registry, [spec()])
    try:
        out = await collect(agent.respond("erase the reports"))
        assert out.startswith("Shall I use demo to erase") and agent.awaiting_confirmation
        out2 = await collect(agent.respond("yes"))
        assert out2 == "Done, sir."
        assert any(r["kind"] == "tool" and r["tool"] == "mcp_demo_erase" and r["ok"] for r in ctx.audit.tail())
    finally:
        await mgr.stop()
