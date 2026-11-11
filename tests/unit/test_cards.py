from datetime import datetime, timezone
from types import SimpleNamespace

from jarvis_core import cards


def test_clean_rejects_unknown_kind_and_empty_cards():
    assert cards.clean({"kind": "nope", "title": "x"}) is None
    assert cards.clean({"kind": "tile"}) is None
    assert cards.clean("not a dict") is None


def test_clean_bounds_everything():
    c = cards.clean({"kind": "list", "items": [{"title": f"t{i}"} for i in range(30)], "title": "x" * 999,
                     "rows": [{"label": "a", "value": "b"}] * 50, "chips": ["c"] * 20, "progress": 7})
    assert len(c["items"]) == cards.MAX_ITEMS and len(c["rows"]) == cards.MAX_ROWS and len(c["chips"]) == cards.MAX_CHIPS
    assert len(c["title"]) <= 140 and c["progress"] == 1.0


def test_actions_only_allow_say_copy_and_http_urls_and_one_primary():
    c = cards.clean({"kind": "tile", "title": "t", "actions": [
        {"label": "Go", "kind": "url", "value": "javascript:alert(1)"},
        {"label": "File", "kind": "url", "value": "file:///etc/passwd"},
        {"label": "Run", "kind": "shell", "value": "rm -rf /"},
        {"label": "Open", "kind": "url", "value": "https://example.com/a", "primary": True},
        {"label": "Say", "kind": "say", "value": "undo that", "primary": True},
        {"label": "Copy", "kind": "copy", "value": "x"}, {"label": "Four", "kind": "copy", "value": "y"}]})
    kinds = [(a["kind"], a["primary"]) for a in c["actions"]]
    assert kinds == [("url", True), ("say", False), ("copy", False)]


def test_icon_must_look_like_an_sf_symbol_name():
    assert cards.clean({"kind": "tile", "title": "t", "icon": "envelope"})["icon"] == "envelope"
    assert "icon" not in cards.clean({"kind": "tile", "title": "t", "icon": "../../x"})


def test_markets_card_signs_and_tones():
    from jarvis_core.briefing.models import Quote

    q = [Quote("^NSEI", "Nifty", 24310.0, 24165.0), Quote("^IXIC", "Nasdaq", 19120.0, 19180.0)]
    rows = cards.clean(cards.markets_card(q))["rows"]
    assert rows[0]["tone"] == "up" and "+" in rows[0]["value"] and rows[1]["tone"] == "down" and "−" in rows[1]["value"]


def test_email_card_has_gmail_link_and_dates():
    item = SimpleNamespace(sender="PESU", title="Boomi drive", thread_id="t1", id="m1",
                           timestamp=datetime(2026, 11, 11, 11, tzinfo=timezone.utc))
    c = cards.clean(cards.email_card(item, "Test on 17th November 2026. CTC up to 14.5 LPA."))
    assert c["actions"][0]["value"].endswith("#all/t1") and c["actions"][0]["primary"]
    assert any("November" in x["text"] for x in c["chips"]) and any("LPA" in x["text"] for x in c["chips"])


def test_auto_cards_from_text_results():
    ctx = SimpleNamespace(state={})
    b = cards.clean(cards.auto_card("battery_status", {}, "Battery at 41 percent, on battery, about 3 hours 5 minutes left.", ctx))
    assert b["big"] == "41%" and b["progress"] == 0.41
    n = cards.clean(cards.auto_card("now_playing", {}, "Weightless by Marconi Union", ctx))
    assert n["kind"] == "media" and n["title"] == "Weightless"
    assert cards.auto_card("battery_status", {}, "Error: nope", ctx) is None
    assert cards.auto_card("now_playing", {}, "Nothing is playing.", ctx) is None


def test_draft_card_never_offers_send():
    c = cards.clean(cards.draft_card("Naan", "WhatsApp", "Running late"))
    assert all("send" not in a["value"].lower() for a in c["actions"]) and "Never sent" in c["footnote"]


def test_tell_me_everything_leads_with_who_they_are_and_never_episodes():
    from jarvis_core.interfaces import Fact
    from jarvis_core.skills.memory_tools import _most_about_them_first

    facts = [Fact(kind="fact", text="I prefer funded startups over bootstrapped ones", created_at=300),
             Fact(kind="fact", text="My name is Kalyan, I go by Kal", created_at=10),
             Fact(kind="preference", text="User likes tea in the evening", created_at=200),
             Fact(kind="fact", text="I live in Bengaluru", created_at=20)]
    texts = [f.text for f in _most_about_them_first(facts)]
    assert texts[:2] == ["My name is Kalyan, I go by Kal", "I live in Bengaluru"]


def test_prompt_facts_never_include_episodes_and_chat_background_is_labelled():
    from jarvis_core.config import Config
    from jarvis_core.brain.persona import build_system_prompt

    p = build_system_prompt(Config(), memory_snippets=["My name is Kalyan"], recent_talk=["Earlier conversation: set a reminder"])
    assert "My name is Kalyan" in p and "NOT facts about them" in p
    assert p.index("NOT facts about them") > p.index("My name is Kalyan")
