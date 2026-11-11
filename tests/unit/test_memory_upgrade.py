"""Memory that stays tidy and finds things: newer facts replace older ones, rare words rank higher, conversations search."""

from __future__ import annotations

import time

import pytest

from jarvis_core.brain.llm import ScriptedLLM
from jarvis_core.config import Config
from jarvis_core.interfaces import Fact
from jarvis_core.memory import reconcile
from jarvis_core.memory.extractor import Extractor, parse_statement
from jarvis_core.memory.store import SQLiteMemory, tokens
from jarvis_core.policy.audit import AuditLog
from jarvis_core.skills import memory_tools as MT
from jarvis_core.skills.base import ToolContext, ToolRegistry


@pytest.fixture
def mem(tmp_path):
    m = SQLiteMemory(tmp_path / "m.db")
    yield m
    m.close()


def ctx_for(mem, tmp_path):
    return ToolContext(cfg=Config(), audit=AuditLog(tmp_path / "audit.log"), services={"memory": mem})


# ------------------------------------------------------------------ reconcile


async def test_a_correction_replaces_the_old_fact(mem):
    await reconcile.apply(mem, Fact(kind="fact", text="My favourite colour is green"))
    out = await reconcile.apply(mem, Fact(kind="fact", text="My favourite colour is blue"))
    assert out.action == "updated" and "green" in out.replaced.text
    assert [f.text for f in mem.list("fact")] == ["My favourite colour is blue"]


async def test_two_things_that_can_both_be_true_are_both_kept(mem):
    await reconcile.apply(mem, Fact(kind="preference", text="I like tea"))
    out = await reconcile.apply(mem, Fact(kind="preference", text="I like coffee"))
    assert out.action == "added" and len(mem.list("preference")) == 2


async def test_the_same_fact_in_other_words_is_not_duplicated(mem):
    await reconcile.apply(mem, Fact(kind="fact", text="I live in Pune"))
    await reconcile.apply(mem, Fact(kind="fact", text="I live in Pune."))
    assert len(mem.list("fact")) == 1


async def test_a_new_person_for_the_same_relation_replaces_the_old_one(mem):
    await reconcile.apply(mem, parse_statement("my brother is Arjun"))
    out = await reconcile.apply(mem, parse_statement("my brother is Rahul"))
    assert out.action == "updated"
    people = mem.list("person")
    assert len(people) == 1 and people[0].meta["name"] == "Rahul"


async def test_different_relations_do_not_collide(mem):
    await reconcile.apply(mem, parse_statement("my brother is Arjun"))
    await reconcile.apply(mem, parse_statement("my sister is Meera"))
    assert len(mem.list("person")) == 2


async def test_episodes_are_never_merged(mem):
    for t in ("[Mon] You said: open spotify | I replied: ok", "[Tue] You said: open spotify | I replied: ok"):
        await reconcile.apply(mem, Fact(kind="episode", text=t))
    assert len(mem.list("episode")) == 2


async def test_the_judge_decides_when_given(mem):
    await reconcile.apply(mem, Fact(kind="fact", text="I work at Google on search"))
    old = mem.list("fact")[0]

    async def replace_it(new, existing):
        return existing[0].id

    async def keep_both(new, existing):
        return None

    out = await reconcile.apply(mem, Fact(kind="fact", text="I work at Meta on search"), judge=keep_both)
    assert out.action == "added" and len(mem.list("fact")) == 2
    out = await reconcile.apply(mem, Fact(kind="fact", text="I work at Meta on search ads"), judge=replace_it)
    assert out.action == "updated" and mem.get(out.id) is not None
    assert old.id in {f.id for f in mem.list("fact")} or out.replaced is not None


async def test_llm_judge_returns_only_known_ids_and_fails_safe(mem):
    old = Fact(id="abc", kind="fact", text="I live in Pune")
    new = Fact(kind="fact", text="I live in Delhi")
    assert await reconcile.llm_judge(ScriptedLLM(['{"replace": "abc"}']))(new, [old]) == "abc"
    assert await reconcile.llm_judge(ScriptedLLM(['{"replace": "zzz"}']))(new, [old]) is None
    assert await reconcile.llm_judge(ScriptedLLM(["not json"]))(new, [old]) is None
    assert await reconcile.llm_judge(ScriptedLLM([]))(new, [old]) is None


async def test_extractor_updates_through_the_judge(mem):
    mem.add(Fact(kind="fact", text="The user lives in Pune"))
    llm = ScriptedLLM(['[{"kind":"fact","text":"The user lives in Delhi","confidence":0.9}]', f'{{"replace": "{mem.list("fact")[0].id}"}}'])
    await Extractor(mem, llm).on_turn_done("I just moved and now I live in Delhi", "Noted")
    assert [f.text for f in mem.list("fact")] == ["The user lives in Delhi"]


async def test_remember_tool_says_what_it_replaced(mem, tmp_path):
    reg = ToolRegistry()
    MT.register(reg)
    ctx = ctx_for(mem, tmp_path)
    remember = reg.get("remember").handler
    assert await remember(ctx, {"text": "my favourite colour is green"}) == "Noted, sir."
    out = await remember(ctx, {"text": "my favourite colour is blue"})
    assert out.startswith("Noted, sir. That replaces what I had:") and "green" in out


# ------------------------------------------------------------------ retrieval


def test_short_forms_fold_together():
    assert tokens("my bro") == tokens("my brother") and tokens("favourite colour") == tokens("favorite color")


def test_rare_words_outrank_common_ones(mem):
    for i in range(6):
        mem.add(Fact(kind="fact", text=f"The user likes thing number {i} a lot"))
    mem.add(Fact(kind="fact", text="The user's dentist is Dr Mehta"))
    assert mem.search("what does the user like about the dentist", k=1)[0].text.endswith("Dr Mehta")


def test_near_spellings_still_match(mem):
    mem.add(Fact(kind="person", text="The user's brother is Arjun", meta={"alias": "brother", "name": "Arjun"}))
    assert mem.search("tell me about Arjan", k=1)


def test_unrelated_queries_find_nothing(mem):
    mem.add(Fact(kind="fact", text="I live in Pune"))
    assert mem.search("quantum chromodynamics") == []


def test_search_can_be_limited_by_kind_and_date(mem):
    now = time.time()
    mem.add(Fact(kind="episode", text="[Mon] You said: book the flight to Goa | I replied: done", created_at=now - 20 * 86400))
    mem.add(Fact(kind="episode", text="[Today] You said: flight status | I replied: on time", created_at=now))
    mem.add(Fact(kind="fact", text="The user prefers window seats on a flight"))
    got = mem.search("flight", k=5, kinds=("episode",), since=now - 7 * 86400)
    assert [f.text for f in got] == ["[Today] You said: flight status | I replied: on time"]


async def test_search_conversations_tool(mem, tmp_path):
    reg = ToolRegistry()
    MT.register(reg)
    ctx = ctx_for(mem, tmp_path)
    tool = reg.get("search_conversations").handler
    assert await tool(ctx, {}) == "We haven't spoken in that time, sir."
    now = ctx.clock().timestamp()
    mem.add(Fact(kind="episode", text="[Mon 10:00] You said: plan the Goa trip | I replied: ok", created_at=now - 3600))
    mem.add(Fact(kind="episode", text="[old] You said: Goa hotels | I replied: ok", created_at=now - 90 * 86400))
    assert "Goa trip" in await tool(ctx, {"query": "goa", "days": 7})
    assert "hotels" not in await tool(ctx, {"query": "goa", "days": 7})
    assert await tool(ctx, {"query": "mars", "days": 7}) == "I don't have a record of that, sir."


def test_synonyms_find_a_reworded_fact(mem):
    mem.add(Fact(kind="fact", text="I was born on 15 Dec 2005 in Vizag"))
    mem.add(Fact(kind="fact", text="I live in Bengaluru"))
    assert mem.search("when is my birthday?", k=1)[0].text.startswith("I was born")


def test_words_that_travel_together_are_learned_from_the_facts(mem):
    for t in ("Zephyr is an AI startup", "Zephyr shut down in October", "Zephyr had two cofounders",
              "I like tea", "I like walks", "I use a Mac"):
        mem.add(Fact(kind="fact", text=t))
    assert mem.search("what happened to the startup", k=2)[0].text.startswith("Zephyr")


def test_short_words_do_not_fuzzy_match_the_wrong_word(mem):
    mem.add(Fact(kind="fact", text="I did Machine Learning at LaunchED"))
    assert mem.search("when do I eat lunch", k=3) == []


def test_search_stays_fast_at_the_fact_cap(mem):
    import time

    for i in range(2000):
        mem.add(Fact(kind="fact", text=f"Fact {i}: I did task{i} with person{i % 40} on project{i % 90} in city{i % 15}"))
    t0 = time.perf_counter()
    mem.search("what startup did I work on with person3 in the city", k=5)
    assert time.perf_counter() - t0 < 0.5


def test_search_quality_on_the_private_fact_pack():
    import importlib.util
    from pathlib import Path

    import pytest

    root = Path(__file__).resolve().parents[2]
    if not (root / "tests/fixtures/memory_pack.txt").exists():
        pytest.skip("private fact pack not present")
    spec = importlib.util.spec_from_file_location("eval_memory", root / "scripts/eval_memory.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    import tempfile

    from jarvis_core.memory.store import SQLiteMemory

    with tempfile.TemporaryDirectory() as d:
        st = SQLiteMemory(Path(d) / "m.db")
        ev.load(st)
        recall, mrr, _, _ = ev.score(st.search)
        held, _, _, _ = ev.score(st.search, cases=ev.HELDOUT)
    assert recall >= 0.80 and held >= 0.60 and mrr >= 0.65
