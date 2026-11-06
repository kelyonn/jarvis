from __future__ import annotations

import json

import pytest
from fakes import Rig

from jarvis_core import runtime as rtmod
from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.comms.contacts import ContactBook
from jarvis_core.comms.setup import memory_aliases
from jarvis_core.config import Config
from jarvis_core.interfaces import Fact
from jarvis_core.memory.cli import forget_cli
from jarvis_core.memory.extractor import Extractor, parse_statement
from jarvis_core.memory.store import SQLiteMemory
from jarvis_core.memory.style import learn_style, style_card
from jarvis_core.policy.audit import AuditLog
from jarvis_core.router import IntentRouter
from jarvis_core.skills import memory_tools as MT
from jarvis_core.skills.base import ToolContext, ToolRegistry


@pytest.fixture
def mem():
    m = SQLiteMemory()
    yield m
    m.close()


def test_add_dedupes_and_searches(mem):
    a = mem.add(Fact(kind="preference", text="The user prefers tea over coffee"))
    assert mem.add(Fact(kind="preference", text="the user prefers TEA over coffee!")) == a
    mem.add(Fact(text="The office is in Koramangala"))
    mem.add(Fact(text="Flight to Delhi on Friday"))
    assert [f.text for f in mem.search("where is my office")] == ["The office is in Koramangala"]
    assert mem.search("what tea do I like")[0].kind == "preference"
    assert mem.search("zebra") == [] and mem.search("the a is") == []
    assert len(mem.list()) == 3 and len(mem.list("preference")) == 1
    with pytest.raises(ValueError):
        mem.add(Fact(text="  !! "))
    mem.delete(a)
    assert mem.get(a) is None


def test_snippets_always_include_preferences_and_skip_style(mem):
    mem.add(Fact(kind="preference", text="Prefers metric units"))
    mem.upsert_style("whatsapp", "On WhatsApp the user writes short messages")
    mem.add(Fact(text="Rahul's birthday is in March"))
    snip = mem.snippets("when is rahul's birthday")
    assert snip[0].startswith("Rahul") and "Prefers metric units" in snip
    assert not any("WhatsApp" in s for s in snip)
    mem.upsert_style("whatsapp", "On WhatsApp the user writes long messages")
    assert [f.text for f in mem.list("style")] == ["On WhatsApp the user writes long messages"]


def test_about_clear_and_cap(mem, monkeypatch):
    mem.add(Fact(text="Rahul owes me lunch"))
    mem.add(Fact(text="Meeting rahulson tomorrow"))  # not a whole-word match
    mem.add(Fact(kind="person", text="The user's brother is Arjun", meta={"alias": "brother", "name": "Arjun"}))
    assert [f.text for f in mem.about("rahul")] == ["Rahul owes me lunch"]
    assert len(mem.about("Arjun")) == 1 and len(mem.about("brother")) == 1
    assert mem.last().kind == "person"
    assert mem.clear() == 3 and mem.list() == []
    monkeypatch.setattr("jarvis_core.memory.store.MAX_FACTS", 3)
    mem.add(Fact(kind="person", text="Mum is Asha", meta={}))
    for i in range(5):
        mem.add(Fact(text=f"transient fact number {i}", created_at=100 + i))
    texts = {f.text for f in mem.list()}
    assert len(texts) == 3 and "Mum is Asha" in texts and "transient fact number 4" in texts


def test_persists_on_disk_privately(mem, jarvis_home):
    mem.add(Fact(text="remember me"))
    again = SQLiteMemory()
    assert [f.text for f in again.list()] == ["remember me"]
    assert (jarvis_home / "memory.db").stat().st_mode & 0o077 == 0
    again.close()


def test_parse_statement():
    f = parse_statement("my brother is Arjun")
    assert (f.kind, f.meta) == ("person", {"alias": "brother", "name": "Arjun"})
    assert parse_statement("that my mum's name is asha.").meta == {"alias": "mum", "name": "Asha"}
    assert parse_statement("my favourite colour is blue").kind == "preference"
    assert parse_statement("my dentist is Dr Rao").kind == "fact"  # not a family/work relation we alias
    f = parse_statement("I prefer window seats")
    assert f.kind == "preference" and f.text == "I prefer window seats"
    assert parse_statement("remind me to follow up with Rahul").kind == "loop"


def test_alias_feeds_contact_resolution(mem):
    ctx = ToolContext(cfg=Config(), services={"memory": mem})
    mem.add(parse_statement("my brother is Arjun"))
    assert memory_aliases(ctx)() == {"brother": "Arjun"}
    book = ContactBook("91", aliases=memory_aliases(ctx))
    assert book._aliases() == {"brother": "Arjun"}


# --------------------------------------------------------------------------------- style


def test_style_card():
    short = ["ok cool", "on my way", "lol see u", "yep", "k thx"]
    card = style_card("whatsapp", short)
    assert "very short" in card and "lowercase" in card and "abbreviations" in card and "WhatsApp" in card
    formal = ["Dear Priya, I hope you are well. Please find the report attached for your review today.",
              "Thank you for the update. I will review the numbers and revert by Thursday afternoon.",
              "Best regards, the figures look correct to me; let us proceed with the plan as discussed."]
    card = style_card("gmail", formal)
    assert "capitalises properly" in card and "punctuation" in card and "email" in card
    assert style_card("gmail", ["a", "b"]) is None


def test_learn_style_upserts(mem):
    assert learn_style(mem, "imessage", ["ok", "sure", "yes"])
    assert learn_style(mem, "imessage", ["Sounds good to me, see you then.", "Great, thank you!", "Will do, cheers."])
    assert len(mem.list("style")) == 1
    assert not learn_style(mem, "imessage", ["x"]) and not learn_style(None, "imessage", ["a", "b", "c"])


# ----------------------------------------------------------------------------- extractor


async def test_extractor_keeps_confident_user_facts_only(mem):
    llm = ScriptedLLM([json.dumps([
        {"kind": "preference", "text": "The user likes filter coffee", "confidence": 0.9},
        {"kind": "fact", "text": "The user is tired", "confidence": 0.3},
        {"kind": "bogus", "text": "The user works at Acme", "confidence": 0.8},
        {"kind": "fact", "text": "x", "confidence": 0.9},
    ])])
    ex = Extractor(mem, llm)
    await ex.on_turn_done("I really like filter coffee in the morning, and I work at Acme", "Noted.")
    assert {f.text for f in mem.list()} == {"The user likes filter coffee", "The user works at Acme"}
    assert llm.calls[0]["role"] == "fast"
    # the model is only ever shown the user's own words, never the assistant's reply / tool data
    assert "Noted." not in json.dumps(llm.calls[0]["messages"])


async def test_extractor_skips_uninteresting_and_bad_output(mem):
    llm = ScriptedLLM(["not json", "[]"])
    ex = Extractor(mem, llm)
    await ex.on_turn_done("what time is it", "")  # no cue, no LLM call
    await ex.on_turn_done("forget that my brother is Arjun please", "")  # explicit command
    await ex.on_turn_done("my sister lives in a very nice flat", "")  # garbage output
    assert llm.calls and len(llm.calls) == 1 and mem.list() == []
    await Extractor(mem, None).on_turn_done("my sister lives in Pune now", "")
    await Extractor(mem, llm, enabled=False).on_turn_done("my sister lives in Pune now", "")
    assert len(llm.calls) == 1


async def test_extractor_person_gets_alias(mem):
    llm = ScriptedLLM([json.dumps([{"kind": "person", "text": "The user's sister is Meera", "confidence": 0.95}])])
    await Extractor(mem, llm).on_turn_done("my sister is Meera and she lives in Pune", "")
    f = mem.list("person")[0]
    assert f.meta == {"alias": "sister", "name": "Meera"}


# --------------------------------------------------------------------------------- tools


def tool_ctx(mem):
    return ToolContext(cfg=Config(), audit=AuditLog(), services={"memory": mem})


async def test_remember_recall_forget_flow(mem):
    reg = ToolRegistry()
    MT.register(reg)
    ctx = tool_ctx(mem)
    assert await reg.get("remember").handler(ctx, {"text": "my brother is Arjun"}) == "Noted, sir. Arjun is your brother."
    assert await reg.get("remember").handler(ctx, {"text": "I prefer tea"}) == "Noted, sir."
    out = await reg.get("recall").handler(ctx, {})
    assert "Arjun" in out and "tea" in out
    assert "brother" in await reg.get("recall").handler(ctx, {"query": "Arjun"})
    assert await reg.get("recall").handler(ctx, {"query": "Zed"}) == "I don't have anything about Zed, sir."
    # "forget that" = the item just stored
    forget = reg.get("forget")
    assert forget.decide(ctx, {}).action == "allow"
    assert "prefer tea" in await forget.handler(ctx, {})
    assert [f.kind for f in mem.list()] == ["person"]
    assert (await forget.handler(ctx, {})).startswith("Forgotten")
    assert await forget.handler(ctx, {}) == "There's nothing to forget, sir."
    assert "don't know much" in await reg.get("recall").handler(ctx, {})


async def test_forgetting_a_person_with_many_facts_asks_first(mem):
    reg = ToolRegistry()
    MT.register(reg)
    ctx = tool_ctx(mem)
    for t in ("Rahul likes cricket", "Rahul lives in Pune", "Rahul's number ends in 42"):
        mem.add(Fact(text=t))
    mem.add(Fact(text="Meera likes chess"))
    forget = reg.get("forget")
    d = forget.decide(ctx, {"query": "Rahul"})
    assert d.action == "confirm" and "3 things about Rahul" in d.prompt
    assert "3 items" in await forget.handler(ctx, {"query": "Rahul"})
    assert [f.text for f in mem.list()] == ["Meera likes chess"]
    assert forget.decide(ctx, {"query": "Meera"}).action == "allow"


async def test_tools_without_memory_and_blank_input():
    reg = ToolRegistry()
    MT.register(reg)
    from jarvis_core.errors import SkillError
    with pytest.raises(SkillError, match="memory isn't available"):
        await reg.get("recall").handler(ToolContext(cfg=Config()), {})
    m = SQLiteMemory()
    with pytest.raises(SkillError, match="What shall I remember"):
        await reg.get("remember").handler(tool_ctx(m), {"text": "  "})
    m.close()


def test_router_memory_intents():
    r = IntentRouter.load()
    def m(t):
        c = r.match(t)
        return (c.tool, c.args) if c else None
    assert m("remember my brother is Arjun") == ("remember", {"text": "my brother is arjun"})
    assert m("Jarvis, remember that I prefer window seats") == ("remember", {"text": "i prefer window seats"})
    assert m("forget that") == ("forget", {})
    assert m("forget everything about Rahul") == ("forget", {"query": "rahul"})
    assert m("what do you know about me") == ("recall", {})
    assert m("what do you know about rahul") == ("recall", {"query": "rahul"})
    assert m("what do you remember") == ("recall", {})


# --------------------------------------------------------------------------------- wiring


async def test_runtime_wires_memory_into_prompt_and_hooks():
    rig = Rig()
    rt = rtmod.Runtime(rig.cfg, rig.daemon, rig.frontend, rig.assistant)
    rtmod._attach_memory(rt, rig.llm)
    mem = rig.assistant.ctx.services["memory"]
    mem.add(Fact(text="The user's office is in Koramangala"))
    assert rig.assistant._prompt_context("where is my office")["memory_snippets"] == ["The user's office is in Koramangala"]
    assert rig.daemon.hooks.on_turn_done
    cfg = Config()
    cfg.memory.enabled = False
    rig2 = Rig(cfg=cfg)
    rt2 = rtmod.Runtime(cfg, rig2.daemon, rig2.frontend, rig2.assistant)
    rtmod._attach_memory(rt2, rig2.llm)
    assert "memory" not in rig2.assistant.ctx.services


def test_forget_cli(mem, capsys):
    mem.add(Fact(text="Rahul likes cricket"))
    mem.add(Fact(text="Meera likes chess"))
    assert forget_cli(person="Rahul") == 0 and "forgot 1" in capsys.readouterr().out
    assert forget_cli() == 2
    assert forget_cli(all_=True) == 0 and "forgot 1" in capsys.readouterr().out


async def test_weekly_style_refresh_learns_from_all_sent_messages(tmp_path):
    from jarvis_core.memory.store import SQLiteMemory
    from jarvis_core.memory.style_refresh import StyleRefresher

    class Reader:
        def all_sent(self, n):
            return ["hey whats up", "lol ok see u there", "cool thx", "yeah sure pls send it"]

    class Hub:
        readers = {"whatsapp": Reader(), "gmail": object()}  # gmail has no bulk reader: skipped

    now = [1_000_000.0]
    mem = SQLiteMemory(tmp_path / "m.db")
    r = StyleRefresher(Hub(), mem, clock=lambda: now[0])
    assert r.due()
    assert await r.refresh() == {"whatsapp": True}
    assert not r.due()
    now[0] += 8 * 86400
    assert r.due()  # a week later


async def test_episodes_are_recalled_and_stay_out_of_the_fact_list(tmp_path):
    import time

    from jarvis_core.memory.extractor import EpisodeRecorder
    from jarvis_core.memory.store import SQLiteMemory

    mem = SQLiteMemory(tmp_path / "m.db")
    rec = EpisodeRecorder(mem)
    await rec.on_turn_done("what do you think about the Ferrari strategy", "Pitting early was the right call.")
    await rec.on_turn_done("ok", "Fine.")  # too short to keep
    assert len(mem.list("episode")) == 1
    assert "Ferrari" in mem.search("that Ferrari thing we discussed")[0].text
    assert "Ferrari" in mem.recent_episodes(3)[0] and time.time()
    mem.close()


def test_conversation_survives_a_restart_only_while_fresh(tmp_path):
    from jarvis_core.brain.history import ConversationHistory

    f, t = tmp_path / "c.json", [1000.0]
    a = ConversationHistory(path=f, expiry_s=600, wall=lambda: t[0])
    a.add({"role": "user", "content": "hi"})
    a.add({"role": "assistant", "content": "hello"})
    a.add({"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]})
    assert [m["content"] for m in ConversationHistory(path=f, expiry_s=600, wall=lambda: t[0] + 100).messages()] == ["hi", "hello"]
    assert ConversationHistory(path=f, expiry_s=600, wall=lambda: t[0] + 700).messages() == []
    assert oct(f.stat().st_mode & 0o777) == "0o600"
