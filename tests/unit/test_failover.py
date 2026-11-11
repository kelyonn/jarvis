import pytest

from jarvis_core.brain.llm import FailoverLLM, ScriptedLLM
from jarvis_core.errors import LLMAuthError, LLMUnavailable
from jarvis_core.interfaces import Done, TextDelta


class Down:
    """An online model that can't be reached."""

    def __init__(self, exc=None):
        self.exc = exc or LLMUnavailable("NIM unreachable: ConnectError")
        self.calls = 0

    async def stream_chat(self, messages, tools=None, **kw):
        self.calls += 1
        raise self.exc
        yield  # pragma: no cover

    def list_models(self):
        return []

    def health(self):
        return False


class Cuts:
    """Streams some words, then drops."""

    async def stream_chat(self, messages, tools=None, **kw):
        yield TextDelta("Half a sentence that was already")
        raise LLMUnavailable("connection dropped")

    def list_models(self):
        return []

    def health(self):
        return True


async def collect(llm, text="hi"):
    return "".join([e.text async for e in llm.stream_chat([{"role": "user", "content": text}]) if isinstance(e, TextDelta)])


def make(primary, reachable_seq, **kw):
    seq = list(reachable_seq)

    async def reach():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    said = []
    llm = FailoverLLM(primary, ScriptedLLM(["Local answer."] * 10), reachable=reach, recheck_s=0.0, **kw)
    llm.notify = said.append
    return llm, said


async def test_offline_switches_to_local_and_says_so_once():
    llm, said = make(Down(), [False])
    assert await collect(llm) == "Local answer."
    assert llm.mode == "local" and len(said) == 1 and "offline" in said[0]
    await collect(llm)
    assert len(said) == 1  # no repeat while it stays local


async def test_online_models_down_but_internet_up_says_that_instead():
    llm, said = make(Down(), [True])
    await collect(llm)
    assert "online models aren't responding" in said[0]


async def test_comes_back_online_and_says_so():
    primary = Down()
    llm, said = make(primary, [False, True])
    await collect(llm)                       # offline: local
    primary.exc = None
    good = ScriptedLLM(["Main answer."])
    llm.primary = good
    assert await collect(llm) == "Main answer."
    assert llm.mode == "online" and "Back online" in said[-1]


async def test_stays_local_while_still_offline():
    primary = Down()
    llm, said = make(primary, [False])
    await collect(llm)
    calls = primary.calls
    await collect(llm)
    assert primary.calls == calls and llm.mode == "local"  # did not hammer the dead connection


async def test_never_restarts_a_half_spoken_answer_on_the_local_model():
    llm, said = make(Cuts(), [False])
    with pytest.raises(LLMUnavailable):
        await collect(llm)
    assert said == []


async def test_bad_api_key_is_not_an_outage():
    llm, said = make(Down(LLMAuthError("bad key")), [False])
    with pytest.raises(LLMAuthError):
        await collect(llm)
    assert said == [] and llm.mode == "online"


async def test_both_down_raises_the_original_error():
    class Dead(ScriptedLLM):
        async def stream_chat(self, *a, **k):
            raise LLMUnavailable("Ollama unreachable")
            yield

    llm = FailoverLLM(Down(), Dead(), reachable=lambda: _false(), recheck_s=0.0)
    with pytest.raises(LLMUnavailable, match="NIM unreachable"):
        await collect(llm)


async def _false():
    return False


async def test_backup_model_switch_is_announced_by_the_nim_client(monkeypatch):
    from jarvis_core.adapters.services import NIMLLM
    from jarvis_core.config import LLMCfg

    cfg = LLMCfg(chat_model="a/primary", fast_model="a/primary", fallback_models=["b/backup"])
    llm = NIMLLM(cfg, "key")
    said = []
    llm.notify = said.append

    async def fake(model, messages, tools, temperature, max_tokens):
        if model == "a/primary":
            raise LLMUnavailable("HTTP 404")
        yield TextDelta("ok")
        yield Done("stop")

    monkeypatch.setattr(llm, "_stream_model", fake)
    _ = [e async for e in llm.stream_chat([{"role": "user", "content": "hi"}])]
    assert said == ["The main model isn't responding, so I've switched to a backup."]
    _ = [e async for e in llm.stream_chat([{"role": "user", "content": "hi"}])]
    assert len(said) == 1  # said once, not on every turn
