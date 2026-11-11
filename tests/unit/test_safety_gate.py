"""Phase 1 safety: one gate for every tool call, capability switches, the fetch guard, secret masking, spoken previews."""

from __future__ import annotations

import httpx
import pytest
from fakes import FakeRunner
from test_skills import call, collect, make_agent, make_ctx

from jarvis_core.config import Config
from jarvis_core.errors import SkillError
from jarvis_core.policy import gate, netguard
from jarvis_core.policy.redact import CARD, SECRET, redact, redact_messages
from jarvis_core.skills.base import Risk, Tool, obj
from jarvis_core.skills.registry import build_registry

# ------------------------------------------------------------------ capability switches


def test_disabled_capability_blocks_its_tools(tmp_path, apps_dir):
    cfg = Config()
    cfg.safety.disabled_capabilities = ["shell"]
    ctx = make_ctx(tmp_path, apps_dir, cfg=cfg)
    run = build_registry().get("run_command")
    assert gate.check(run, ctx, {"command": "ls"}).action == "block"
    assert gate.check(build_registry().get("open_app"), ctx, {"name": "notes"}).action == "allow"


def test_switch_works_for_unknown_names_as_categories(tmp_path, apps_dir):
    cfg = Config()
    cfg.safety.disabled_capabilities = ["Timers"]
    assert gate.disabled_categories(cfg) == {"timers"}


def test_disabled_tools_are_hidden_from_the_model():
    reg = build_registry()
    names = {s["function"]["name"] for s in reg.schemas(exclude={"terminal"})}
    assert "run_command" not in names and "open_app" in names


async def test_agent_refuses_a_disabled_tool_even_if_the_model_calls_it(tmp_path, apps_dir):
    cfg = Config()
    cfg.safety.disabled_capabilities = ["shell"]
    agent, llm, ctx = make_agent(tmp_path, apps_dir, [[call("run_command", {"command": "ls"})], "That is switched off, sir."], cfg=cfg)
    out = await collect(agent.respond("list my files"))
    assert out == "That is switched off, sir." and not ctx.runner.calls
    assert any(r["kind"] == "blocked" and r["tool"] == "run_command" for r in ctx.audit.tail())


async def test_direct_router_path_respects_the_gate(tmp_path, apps_dir):
    cfg = Config()
    cfg.safety.disabled_capabilities = ["apps"]
    agent, _, ctx = make_agent(tmp_path, apps_dir, [], cfg=cfg)

    class Direct:
        tool, args = "open_app", {"name": "notes"}

    assert await agent.try_direct("open notes", Direct()) is None
    assert not ctx.runner.calls


async def test_invoke_refuses_blocked_tools_whatever_called_it(tmp_path, apps_dir):
    cfg = Config()
    cfg.safety.disabled_capabilities = ["apps"]
    agent, _, ctx = make_agent(tmp_path, apps_dir, [], cfg=cfg)
    out = await agent._invoke(build_registry().get("open_app"), {"name": "notes"})
    assert out.startswith("Error:") and "turned off" in out and not ctx.runner.calls


# ------------------------------------------------------------------ fetch guard


def _resolver(mapping):
    def resolve(host, port):
        if host not in mapping:
            raise OSError("no such host")
        return [(2, 1, 6, "", (ip, 0)) for ip in mapping[host]]

    return resolve


PUBLIC = _resolver({"example.com": ["93.184.216.34"], "evil.test": ["10.0.0.5"], "mixed.test": ["93.184.216.34", "127.0.0.1"]})


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/admin", "http://localhost/x", "http://169.254.169.254/latest/meta-data",
    "http://192.168.1.1/", "http://10.1.2.3/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://evil.test/",
    "http://mixed.test/", "http://100.64.0.1/", "http://0.0.0.0/", "http://unknown.invalid/",
])
def test_private_and_unresolvable_addresses_are_refused(url):
    with pytest.raises(SkillError):
        netguard.check_public_url(url, PUBLIC)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "http://user:pw@example.com/"])
def test_non_web_and_login_urls_are_refused(url):
    with pytest.raises(SkillError):
        netguard.check_public_url(url, PUBLIC)


def test_public_url_is_accepted():
    assert netguard.check_public_url("https://example.com/a?b=1", PUBLIC) == "https://example.com/a?b=1"


def test_redirect_to_a_private_address_is_not_followed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    with pytest.raises(SkillError):
        netguard.fetch_public("https://example.com/", resolver=PUBLIC, transport=httpx.MockTransport(handler))
    assert seen == ["https://example.com/"]  # the second hop was never requested


def test_public_redirect_chain_is_followed_and_body_returned():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, text="hello page")

    out = netguard.fetch_public("https://example.com/a", resolver=PUBLIC, transport=httpx.MockTransport(handler))
    assert out == "hello page"


def test_redirect_loop_gives_up():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    with pytest.raises(SkillError):
        netguard.fetch_public("https://example.com/", resolver=PUBLIC, transport=httpx.MockTransport(handler))


def test_body_is_size_capped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50_000)

    out = netguard.fetch_public("https://example.com/", resolver=PUBLIC, transport=httpx.MockTransport(handler), max_bytes=1000)
    assert len(out) == 1000


def test_fetch_url_reports_a_refusal_instead_of_crashing():
    from jarvis_core.adapters.services import fetch_url

    assert fetch_url("http://127.0.0.1:9/").startswith("Could not fetch page:")


# ------------------------------------------------------------------ secret masking


@pytest.mark.parametrize("text", [
    "key AKIAIOSFODNN7EXAMPLE here",
    "token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "slack xoxb-123456789012-abcdefghij",
    "nim nvapi-" + "x" * 40,
    "sk-" + "Ab1" * 12,
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
    "password: hunter2hunter",
    "api_key=\"abcdef123456\"",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----",
])
def test_secrets_are_masked(text):
    out, n = redact(text)
    assert n >= 1 and SECRET in out
    for leaked in ("AKIAIOSFODNN7EXAMPLE", "hunter2hunter", "abcdef123456", "MIIBOgIBAAJBAK", "dBjftJeZ4CVPmB92K27uhbUJU1p1r"):
        assert leaked not in out


def test_label_is_kept_when_a_value_is_masked():
    assert redact("my password: hunter2hunter ok")[0] == f"my password: {SECRET} ok"


def test_card_numbers_are_masked_only_when_they_pass_the_luhn_check():
    out, n = redact("card 4111 1111 1111 1111 and order 1234 5678 9012 3456")
    assert CARD in out and "4111" not in out and "1234 5678 9012 3456" in out and n == 1


@pytest.mark.parametrize("text", [
    "Call Rahul on +91 98765 43210 about rahul@example.com", "Meet at 3pm, room 4022", "The token of appreciation was small",
    "Order 20231114 shipped", "pi is 3.14159265358979",
])
def test_ordinary_text_is_left_alone(text):
    assert redact(text) == (text, 0)


def test_messages_are_masked_in_content_parts_and_tool_arguments_without_touching_the_originals():
    msgs = [
        {"role": "user", "content": "my password: hunter2hunter"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "x", "arguments": '{"k": "AKIAIOSFODNN7EXAMPLE"}'}}]},
        {"role": "user", "content": [{"type": "text", "text": "pwd=abcdef123456"}, {"type": "image_url", "image_url": {"url": "data:..."}}]},
    ]
    out, n = redact_messages(msgs)
    assert n == 3
    assert SECRET in out[0]["content"] and "AKIA" not in out[1]["tool_calls"][0]["function"]["arguments"]
    assert SECRET in out[2]["content"][0]["text"] and out[2]["content"][1]["type"] == "image_url"
    assert "hunter2hunter" in msgs[0]["content"]  # input unchanged


async def test_nim_client_masks_secrets_before_sending(monkeypatch):
    from jarvis_core.adapters.services import NIMLLM
    from jarvis_core.config import LLMCfg
    from jarvis_core.interfaces import TextDelta

    sent = []

    async def fake_stream(self, model, messages, tools, temperature, max_tokens):
        sent.append(messages)
        yield TextDelta("ok")

    monkeypatch.setattr(NIMLLM, "_stream_model", fake_stream)
    llm = NIMLLM(LLMCfg(), "key")
    _ = [e async for e in llm.stream_chat([{"role": "user", "content": "password: hunter2hunter"}])]
    assert SECRET in sent[0][0]["content"] and "hunter2hunter" not in sent[0][0]["content"]

    sent.clear()
    off = LLMCfg()
    off.redact_secrets = False
    _ = [e async for e in NIMLLM(off, "key").stream_chat([{"role": "user", "content": "password: hunter2hunter"}])]
    assert "hunter2hunter" in sent[0][0]["content"]


# ------------------------------------------------------------------ spoken previews


def _tool(**kw):
    return Tool("do_thing", "x", obj(), lambda c, a: None, risk=Risk.CONFIRM, **kw)


def test_default_confirmation_says_what_will_happen(tmp_path, apps_dir):
    ctx = make_ctx(tmp_path, apps_dir, FakeRunner())
    assert _tool().decide(ctx, {}).prompt == "Shall I go ahead with do thing?"
    p = _tool().decide(ctx, {"target": "Quarterly report.pdf", "count": 3, "flag": True, "note": ""}).prompt
    assert p == "Shall I go ahead with do thing, Quarterly report.pdf and 3?"
    long = _tool().decide(ctx, {"a": "x" * 100}).prompt
    assert long.endswith("...?") and len(long) < 80
    assert _tool(confirm_prompt=lambda a: "Custom?").decide(ctx, {"a": "b"}).prompt == "Custom?"
