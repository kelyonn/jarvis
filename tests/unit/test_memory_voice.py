import pytest

from jarvis_core.interfaces import Fact
from jarvis_core.memory.voice import second_person


@pytest.mark.parametrize("stored,spoken", [
    ("My name is Asha Rao, I go by Ash", "Your name is Asha Rao, you go by Ash"),
    ("I was born on 15 Dec 2005", "You were born on 15 Dec 2005"),
    ("I'm 20 as of Nov 2026 and I turn 21", "You're 20 as of Nov 2026 and you turn 21"),
    ("I've got a Mac and I'll keep it", "You've got a Mac and you'll keep it"),
    ("People also know me online as kelyonn.", "People also know you online as kelyonn."),
    ("User prefers simple shots", "You prefer simple shots"),
    ("User has code that needs fixing", "You have code that needs fixing"),
    ("The user's brother is Arjun.", "Your brother is Arjun."),
    ("I prefer tea", "You prefer tea"),
])
def test_stored_first_person_is_spoken_to_them_as_you(stored, spoken):
    assert second_person(stored) == spoken


def test_already_second_person_is_left_alone():
    assert second_person("You like tea") == "You like tea"
    assert second_person("") == ""


async def _ctx(facts):
    from jarvis_core.config import Config
    from jarvis_core.skills.base import ToolContext

    class Mem:
        def list(self, kind=None):
            return facts

        def about(self, q):
            return []

        def search(self, q, k=5, *, kinds=None, since=None):
            return [f for f in facts if (kinds is None or f.kind in kinds) and q in f.text.lower()]

    ctx = ToolContext(cfg=Config())
    ctx.services["memory"] = Mem()
    return ctx


async def test_overview_is_second_person_short_and_skips_stale_duplicates_and_chats():
    from jarvis_core.skills.memory_tools import _recall

    facts = [Fact(kind="fact", text="My name is Kalyan, I go by Kal", created_at=1),
             Fact(kind="fact", text="I live in Bengaluru", created_at=2),
             Fact(kind="fact", text="I'm in sem 7 as of Nov 2026 and I have no classes", created_at=3),
             Fact(kind="fact", text="Nov 2026: I'm in sem 7 and I have no classes at all", created_at=4),
             Fact(kind="episode", text="You said: remind me | you replied: Would you like me to read them?", created_at=5)] + [
        Fact(kind="fact", text=f"I enjoy hobby number {i} a lot", created_at=10 + i) for i in range(30)]
    out = await _recall(await _ctx(facts), {})
    assert out.startswith("Your name is Kalyan, you go by Kal. You live in Bengaluru.")
    assert " my " not in out.lower() and " I " not in out and "Would you like" not in out
    assert out.count("sem 7") == 1  # the dated near-duplicate is dropped
    assert out.count(".") <= 10  # a few sentences, not all 30 hobbies


async def test_topic_search_never_returns_chat_logs():
    from jarvis_core.skills.memory_tools import _recall

    facts = [Fact(kind="episode", text="You said: college | you replied: Would you like me to read?", created_at=1),
             Fact(kind="fact", text="I study at PES, my college", created_at=2)]
    out = await _recall(await _ctx(facts), {"query": "college"})
    assert "Would you like" not in out and "Your college" in out or "your college" in out
