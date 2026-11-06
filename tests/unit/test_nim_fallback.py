from __future__ import annotations

import pytest

from jarvis_core.adapters.services import NIMLLM
from jarvis_core.config import LLMCfg
from jarvis_core.errors import LLMAuthError, LLMRateLimited, LLMUnavailable
from jarvis_core.interfaces import Done, TextDelta


def make(monkeypatch, behaviour: dict[str, object], **cfg_kw) -> tuple[NIMLLM, list[str]]:
    cfg = LLMCfg(chat_model="a/primary", fast_model="a/primary", vision_model="a/primary",
                 fallback_models=["b/second", "c/third"], **cfg_kw)
    llm = NIMLLM(cfg, "nvapi-test")
    llm._engine = None
    tried: list[str] = []

    async def fake(model, messages, tools, temperature, max_tokens):
        tried.append(model)
        action = behaviour[model]
        if isinstance(action, Exception):
            raise action
        for chunk in action:
            yield chunk
        yield Done("stop")

    monkeypatch.setattr(llm, "_stream_model", fake)
    return llm, tried


async def collect(llm, role="chat"):
    return "".join([e.text async for e in llm.stream_chat([{"role": "user", "content": "hi"}], role=role) if isinstance(e, TextDelta)])


async def test_dead_primary_falls_through_and_is_remembered(monkeypatch):
    llm, tried = make(monkeypatch, {"a/primary": LLMUnavailable("NIM returned HTTP 404"), "b/second": [TextDelta("hello")]})
    assert await collect(llm) == "hello" and tried == ["a/primary", "b/second"]
    tried.clear()
    assert await collect(llm) == "hello" and tried == ["b/second"]  # goes straight to what worked
    assert llm.candidates("chat")[0] == "b/second"


async def test_all_dead_raises_and_clears_memory(monkeypatch):
    llm, tried = make(monkeypatch, {m: LLMUnavailable(f"{m} gone") for m in ("a/primary", "b/second", "c/third")})
    with pytest.raises(LLMUnavailable, match="c/third gone"):
        await collect(llm)
    assert tried == ["a/primary", "b/second", "c/third"]


async def test_auth_and_rate_limit_do_not_switch_models(monkeypatch):
    llm, tried = make(monkeypatch, {"a/primary": LLMAuthError("bad key"), "b/second": [TextDelta("x")]})
    with pytest.raises(LLMAuthError):
        await collect(llm)
    assert tried == ["a/primary"]
    llm, tried = make(monkeypatch, {"a/primary": LLMRateLimited("slow down"), "b/second": [TextDelta("x")]})
    with pytest.raises(LLMRateLimited):
        await collect(llm)
    assert tried == ["a/primary"]


async def test_no_fallback_after_output_started(monkeypatch):
    llm, tried = make(monkeypatch, {"a/primary": [TextDelta("par")], "b/second": [TextDelta("x")]})

    async def breaks(model, messages, tools, temperature, max_tokens):
        tried.append(model)
        yield TextDelta("partial")
        raise LLMUnavailable("connection dropped")

    monkeypatch.setattr(llm, "_stream_model", breaks)
    got = []
    with pytest.raises(LLMUnavailable):
        async for e in llm.stream_chat([{"role": "user", "content": "hi"}]):
            got.append(e)
    assert tried == ["a/primary"] and got  # a half-spoken answer is never restarted on another model


async def test_probe_reports_without_falling_back(monkeypatch):
    llm, tried = make(monkeypatch, {"a/primary": LLMUnavailable("NIM returned HTTP 410"), "b/second": [TextDelta("OK")]})
    assert await llm.probe("a/primary") == (False, "NIM returned HTTP 410")
    assert await llm.probe("b/second") == (True, "answers")
    assert tried == ["a/primary", "b/second"]


def test_defaults_are_the_probed_models():
    cfg = LLMCfg()
    assert cfg.chat_model.startswith("nvidia/nemotron") and cfg.fallback_models
    assert cfg.chat_model not in cfg.fallback_models


async def test_models_rejecting_extra_body_are_retried_plain(monkeypatch):
    cfg = LLMCfg(fallback_models=[])
    llm = NIMLLM(cfg, "nvapi-test")
    seen: list[dict] = []

    async def once(model, messages, tools, temperature, max_tokens, extra):
        seen.append(dict(extra))
        if extra:
            raise LLMUnavailable("NIM returned HTTP 400")
        yield TextDelta("fine")
        yield Done("stop")

    monkeypatch.setattr(llm, "_stream_once", once)
    out = [e async for e in llm._stream_model("m/x", [], None, None, None)]
    assert any(isinstance(e, TextDelta) for e in out)
    assert seen[0] == cfg.extra_body and seen[1] == {}
    seen.clear()
    [e async for e in llm._stream_model("m/x", [], None, None, None)]
    assert seen == [{}]  # remembered: no more rejected requests
    assert cfg.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}
