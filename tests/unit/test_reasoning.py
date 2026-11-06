"""Per-turn mode, reasoning technique, hidden thinking and dynamic prompts."""

from datetime import datetime

import pytest

from jarvis_core.brain.modes import classify_kind, plan_turn
from jarvis_core.brain.persona import build_system_prompt
from jarvis_core.brain.thinking import ThinkFilter, strip_think
from jarvis_core.config import Config
from tests.unit.test_skills import collect, make_agent


@pytest.mark.parametrize("text,kind", [
    ("hello how are you", "chat"),
    ("open safari", "action"),
    ("set a timer for ten minutes", "action"),
    ("what's the capital of australia", "lookup"),
    ("explain how a transformer attention head works", "explain"),
    ("write a python function to merge intervals", "code"),
    ("what is 17% of 2340", "math"),
    ("should I take the offer in Bangalore or stay and ask for a raise", "analysis"),
    ("react vs vue", "analysis"),
    ("my wifi keeps dropping, why is this happening", "troubleshoot"),
    ("my wifi drops every few minutes but only on my laptop. what's going on", "troubleshoot"),
    ("a train leaves at 3:15 going 80 km/h and another at 4:00 going 100 km/h, when does the second catch the first", "math"),
    ("draft a reply to Naan saying I'll send the notes at eight", "draft"),
    ("summarize my unread messages", "summarize"),
    ("brainstorm names for my podcast", "creative"),
])
def test_kind(text, kind):
    assert classify_kind(text) == kind


def test_short_followups_keep_the_topic():
    assert classify_kind("why?", "explain") == "explain"
    assert classify_kind("and the second one?", "analysis") == "analysis"
    assert classify_kind("why?", "chat") == "chat"


def test_techniques_match_the_job():
    assert plan_turn("hello there").technique == "direct"
    assert plan_turn("open safari").think_words == 0
    m = plan_turn("what is 17% of 2340")
    assert m.technique == "cot" and m.verify and m.temperature <= 0.2 and m.thinks
    a = plan_turn("should I take the offer in Bangalore or stay and ask for a raise, given the commute, the equity and my sister being here")
    assert a.technique == "tot" and a.thinks
    assert plan_turn("react vs vue for a small team").technique == "direct"  # quick decisions skip the first pass
    big = plan_turn("plan my week: launch Thursday, two interviews, gym, my sister visits Saturday, and I also need to prep slides")
    assert big.technique in ("ltm", "got") and big.complexity in ("medium", "high")
    hard = plan_turn("think carefully about whether to rewrite our backend in Rust, considering the team, the timeline, the risks "
                     "and the hiring market, and then tell me what to do first", "chat")
    assert hard.technique == "got" and hard.complexity == "high"


def test_reasoning_can_be_switched_off():
    p = plan_turn("what is 17% of 2340", reasoning=False)
    assert p.think_words == 0 and not p.thinks and not p.verify


def test_budget_is_capped():
    p = plan_turn("think carefully about the architecture trade-offs, constraints and edge cases of several designs, then pick", max_think_words=90)
    assert p.think_words <= 90


def test_think_filter_streaming():
    f = ThinkFilter()
    out = "".join(f.feed(p) for p in ["<thi", "nk>work it out\nstep two</th", "ink>\nThe answer is 4."]) + f.flush()
    assert out == "The answer is 4." and "step two" in f.thought


def test_think_filter_unclosed_falls_back_to_the_last_paragraph():
    f = ThinkFilter()
    assert f.feed("<think>hmm\n\nso it is 5") + f.flush() == "so it is 5"


def test_unclosed_after_an_answer_is_dropped():
    f = ThinkFilter()
    assert f.feed("Here you go. <think>and also") + f.flush() == "Here you go. "


def test_strip_think_plain_text_untouched():
    assert strip_think("No tags <3 here") == "No tags <3 here"


def test_prompt_is_dynamic():
    cfg = Config()
    base = build_system_prompt(cfg, now=datetime(2026, 11, 5, 14, 0).astimezone())
    assert "This turn:" not in base
    math = build_system_prompt(cfg, plan=plan_turn("solve x^2-5x+6=0"), user_text="solve x^2-5x+6=0", notes="x=2 or 3")
    assert "calculate tool" in math and "<notes>" in math and "x=2 or 3" in math
    nonotes = build_system_prompt(cfg, plan=plan_turn("how are you"), user_text="how are you")
    assert "<notes>" not in nonotes and "chat request" in nonotes
    late = build_system_prompt(cfg, now=datetime(2026, 11, 5, 23, 40).astimezone())
    assert "It's late" in late
    focus = build_system_prompt(cfg, focus="Work")
    assert "Work focus" in focus
    follow = build_system_prompt(cfg, turn_index=2, interrupted=True)
    assert "follow-up" in follow and "interrupted" in follow


def test_deliberation_prompt_uses_the_technique():
    from jarvis_core.brain.modes import deliberation_messages

    q = "should I take the offer in Bangalore or stay and ask for a raise, given the commute, the equity and my sister being here"
    tot = deliberation_messages(plan_turn(q), q)
    assert "three genuinely different options" in tot[0]["content"] and tot[-1]["content"].startswith("should I")
    cot = deliberation_messages(plan_turn("solve x^2-5x+6=0"), "solve x^2-5x+6=0")
    assert "step by step" in cot[0]["content"] and "Conclusion:" in cot[0]["content"]


async def test_agent_thinks_privately_then_answers(tmp_path, apps_dir):
    agent, llm, _ = make_agent(tmp_path, apps_dir, ["0.17*2340 = 397.8. Conclusion: about 398.", "<think>fine</think>It's about 398."])
    out = await collect(agent.respond("what is 17% of 2340"))
    assert out == "It's about 398."
    assert "think" not in str(agent.history.messages()) and "397.8" not in str(agent.history.messages())
    notes_call, answer_call = llm.calls
    assert notes_call["role"] == "fast" and "step by step" in notes_call["messages"][0]["content"]
    assert answer_call["temperature"] == 0.1 and answer_call["max_tokens"] > 700
    assert "<notes>" in answer_call["messages"][0]["content"] and "397.8" in answer_call["messages"][0]["content"]


async def test_agent_answers_even_if_thinking_fails(tmp_path, apps_dir):
    agent, llm, _ = make_agent(tmp_path, apps_dir, ["unused"])

    async def boom(*a, **k):
        raise RuntimeError("down")

    llm.complete = boom
    out = await collect(agent.respond("what is 17% of 2340"))
    assert out == "unused" and "<notes>" not in llm.calls[0]["messages"][0]["content"]


async def test_reasoning_off_means_no_first_pass(tmp_path, apps_dir):
    cfg = Config()
    cfg.llm.reasoning = False
    agent, llm, _ = make_agent(tmp_path, apps_dir, ["It's about 398."], cfg=cfg)
    await collect(agent.respond("what is 17% of 2340"))
    assert len(llm.calls) == 1


async def test_agent_skips_thinking_for_small_talk(tmp_path, apps_dir):
    agent, llm, _ = make_agent(tmp_path, apps_dir, ["Doing well, you?"])
    await collect(agent.respond("hello how are you"))
    assert len(llm.calls) == 1 and "<notes>" not in llm.calls[0]["messages"][0]["content"]
    assert llm.calls[0]["max_tokens"] == 350


async def test_followup_inherits_the_previous_kind(tmp_path, apps_dir):
    agent, llm, _ = make_agent(tmp_path, apps_dir, ["Notes. Conclusion: Vue.", "Pick Vue.", "Smaller learning curve."])
    await collect(agent.respond("should I use react or vue for a small team"))
    await collect(agent.respond("why?"))
    assert agent.plan.kind == "analysis"


def test_speech_cap_per_kind():
    assert plan_turn("hello there").max_sentences <= 4
    assert plan_turn("write a python function to merge intervals").max_sentences == 2
    assert plan_turn("open safari").max_sentences == 2


def test_speaker_voices_only_the_cap_but_shows_everything():
    import asyncio

    from jarvis_core.frontend import FakeFrontend
    from jarvis_core.voice.speaker import Speaker
    from tests.fakes import FakeTTS

    fe, tts = FakeFrontend(), FakeTTS()
    sp = Speaker(tts, fe)
    sp.cap_source = lambda: 2

    async def stream():
        yield "First point here. Second point follows. Third point too. Fourth point last."

    spoken = asyncio.run(sp.speak_stream(stream()))
    assert spoken == "First point here. Second point follows."
    assert len(tts.spoken) == 2
    assert "Fourth point last." in fe.captions[-1]
    assert sp.cap_source is None
