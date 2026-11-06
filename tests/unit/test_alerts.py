from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jarvis_core.alerts import rules as R
from jarvis_core.alerts.engine import AlertEngine, compose_batch
from jarvis_core.alerts.models import Alert
from jarvis_core.briefing.collectors import calendar as cal
from jarvis_core.briefing.collectors import markets as mk
from jarvis_core.briefing.collectors import news as nw
from jarvis_core.briefing.collectors import weather as wx
from jarvis_core.briefing.env import Env
from jarvis_core.briefing.models import CalEvent, Headline, Quote, Weather
from jarvis_core.briefing.store import Store
from jarvis_core.config import Config
from jarvis_core.interfaces import Item
from jarvis_core.policy.presence import Presence, read_focus
from jarvis_core.policy.queue import AlertQueue
from jarvis_core.router import IntentRouter
from jarvis_core.skills.alerts_tools import register
from jarvis_core.skills.base import ToolContext, ToolRegistry

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m=0, day=2):
    return datetime(2026, 11, day, h, m, tzinfo=IST)


class Clock:
    def __init__(self, start=None):
        self.t = (start or at(12)).timestamp()

    def __call__(self):
        return self.t

    def dt(self):
        return datetime.fromtimestamp(self.t, IST)

    def advance(self, minutes):
        self.t += minutes * 60


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
async def env(store, clock):
    e = Env(cfg=Config(), http=Env.make_client(), store=store, ctx=ToolContext(cfg=Config()), now=clock.dt)
    yield e
    await e.http.aclose()


# --------------------------------------------------------------------------- rules


async def test_meeting_rule_alerts_only_inside_lead_window(env, monkeypatch, clock):
    async def events(e, start, end):
        evs = [CalEvent("a", "Standup", clock.dt() + timedelta(minutes=8), None, location="Room 4"),
               CalEvent("b", "Later", clock.dt() + timedelta(minutes=40), None),
               CalEvent("c", "Holiday", clock.dt() + timedelta(minutes=5), None, all_day=True),
               CalEvent("d", "Started", clock.dt() - timedelta(minutes=6), None)]
        return [ev for ev in evs if start - timedelta(minutes=1) <= ev.start <= end]

    monkeypatch.setattr(cal, "events_between", events)
    out = await R.MeetingRule().check(env)
    assert [a.payload["title"] for a in out] == ["Standup"]
    a = out[0]
    assert a.priority == "high" and "8 minutes" in a.speech and "Room 4" in a.speech
    assert a.expires and a.expires > clock() + 8 * 60


def test_meeting_speech_rerenders_at_delivery(clock):
    a = Alert("k", "meeting", "t", "stale", payload={"start": clock() + 600, "title": "Standup"})
    from jarvis_core.alerts.engine import render_speech

    assert "10 minutes" in render_speech(a, clock())
    assert "2 minutes" in render_speech(a, clock() + 480)
    assert "starting now" in render_speech(a, clock() + 700)


class Hub:
    def __init__(self, items, vips=()):
        self.items, self.vips = items, set(vips)

    async def unread(self, since, sources):
        return [i for i in self.items if i.source in sources]

    def importance(self, item):
        vip = item.sender in self.vips
        return (3 if vip else 1), vip


async def test_vip_rule_only_vips_and_offers_reply(env):
    env.services["comms"] = Hub(
        [Item("whatsapp", "1", sender="Priya", body="Call me when you can"),
         Item("whatsapp", "2", sender="Stranger", body="hi"),
         Item("gmail", "3", sender="Boss", title="Contract")], vips={"Priya", "Boss"})
    out = await R.VipRule().check(env)
    assert {a.key for a in out} == {"vip:whatsapp:1", "vip:gmail:3"}
    wa = next(a for a in out if a.key.endswith(":1"))
    assert "Priya" in wa.speech and "WhatsApp" in wa.speech and wa.speech.endswith("Shall I draft a reply?")
    assert wa.priority == "urgent" and wa.follow_up and wa.payload["offer"]["item_id"] == "1"
    env.cfg.messaging.proactive_reply_offers = False
    assert all("draft" not in a.speech for a in await R.VipRule().check(env))


async def test_vip_rule_without_hub_is_silent(env):
    assert await R.VipRule().check(env) == []


def quote(sym, price, prev, name=None):
    return Quote(sym, name or sym, price, prev)


async def test_market_rule_thresholds_and_escalation(env, monkeypatch):
    state = {"q": [quote("^NSEI", 101.0, 100.0, "Nifty 50"), quote("BTC-USD", 103.0, 100.0, "Bitcoin")]}

    async def collect(e, symbols=None):
        return state["q"]

    monkeypatch.setattr(mk, "collect", collect)
    assert await R.MarketRule().check(env) == []  # +1% index, +3% crypto: under thresholds
    state["q"] = [quote("^NSEI", 98.0, 100.0, "Nifty 50"), quote("BTC-USD", 106.0, 100.0, "Bitcoin")]
    out = await R.MarketRule().check(env)
    assert [a.payload["symbol"] for a in out] == ["BTC-USD", "^NSEI"]  # biggest mover first
    assert "dropped 2.0 percent" in out[1].speech
    same = await R.MarketRule().check(env)
    assert [a.key for a in same] == [a.key for a in out]  # stable keys so the engine can de-dupe
    state["q"] = [quote("^NSEI", 96.0, 100.0, "Nifty 50")]
    worse = await R.MarketRule().check(env)
    assert worse[0].key != out[1].key  # 4% is a new bucket: alert again


async def test_rain_rule(env, monkeypatch):
    async def collect(e, city=None):
        return Weather("Bengaluru", 24, 25, "clear", 30, 22, 70, rain_next_hours=[(1, 20), (2, 75)])

    monkeypatch.setattr(wx, "collect", collect)
    out = await R.RainRule().check(env)
    assert len(out) == 1 and "about 2 hours" in out[0].speech and "75 percent" in out[0].speech
    env.cfg.alerts.rain_lookahead_hours = 1
    assert await R.RainRule().check(env) == []


def feed(items):
    return [Headline(t, src, published=at(11, 30)) for t, src in items]


async def test_breaking_news_needs_enough_sources_and_is_deduped(env, monkeypatch, store):
    story = "Powerful earthquake strikes northern Japan coast tsunami warning issued"
    items = feed([(story, "BBC"), (story + " residents", "Reuters"), ("Tsunami warning issued after powerful earthquake strikes northern Japan", "Hindu"),
                  ("Local bakery wins award", "BBC")])

    async def fetch(e, url):
        return items

    monkeypatch.setattr(nw, "fetch_feed", fetch)
    out = await R.BreakingNewsRule().check(env)
    assert len(out) == 1 and out[0].speech.startswith("Breaking, sir.") and "3 outlets" in out[0].speech
    store.mark_seen("news_alert", out[0].payload["seen_ids"])
    assert await R.BreakingNewsRule().check(env) == []
    env.cfg.alerts.breaking_news_min_sources = 4
    store._db.execute("DELETE FROM seen")
    assert await R.BreakingNewsRule().check(env) == []


async def test_breaking_news_ignores_stale_stories(env, monkeypatch):
    story = "Powerful earthquake strikes northern Japan coast tsunami warning issued"
    old = [Headline(story, s, published=at(6)) for s in ("BBC", "Reuters", "Hindu")]

    async def fetch(e, url):
        return old

    monkeypatch.setattr(nw, "fetch_feed", fetch)
    assert await R.BreakingNewsRule().check(env) == []


async def test_battery_rule(env):
    def sensor(pct, plugged):
        return lambda: SimpleNamespace(percent=pct, power_plugged=plugged)

    assert await R.BatteryRule(sensor(14, True)).check(env) == []
    assert await R.BatteryRule(sensor(40, False)).check(env) == []
    assert await R.BatteryRule(lambda: None).check(env) == []
    (low,) = await R.BatteryRule(sensor(14, False)).check(env)
    assert low.priority == "high" and "14 percent" in low.speech
    (crit,) = await R.BatteryRule(sensor(4, False)).check(env)
    assert crit.priority == "urgent" and crit.key != low.key


# ------------------------------------------------------------------------- presence


def test_presence_states(clock):
    focus = {"v": (False, "")}
    p = Presence(focus_reader=lambda: focus["v"], clock=clock, focus_cache_s=0)
    assert p.state() == "free" and p.can_speak()
    p.on_device_usage(True, False, "zoom.us")
    assert p.state() == "call" and not p.can_speak()
    p.on_device_usage(False, False, "")
    focus["v"] = (True, "Work")
    assert p.state() == "focus" and p.focus_label() == "Work"
    focus["v"] = (None, "")  # unreadable: don't block alerts
    assert p.state() == "free"
    p.snooze(30)
    assert p.state() == "snoozed"
    clock.advance(31)
    assert p.state() == "free"


def test_read_focus_parses_assertions(tmp_path):
    a, m = tmp_path / "Assertions.json", tmp_path / "ModeConfigurations.json"
    assert read_focus(a, m)[0] in (None, False)  # missing file: legacy fallback, never raises
    a.write_text(json.dumps({"data": [{"storeAssertionRecords": []}]}))
    assert read_focus(a, m) == (False, "")
    a.write_text(json.dumps({"data": [{"storeAssertionRecords": [
        {"assertionDetails": {"assertionDetailsModeIdentifier": "com.apple.focus.work"}}]}]}))
    m.write_text(json.dumps({"data": [{"modeConfigurations": {"x": {"mode": {"modeIdentifier": "com.apple.focus.work", "name": "Work"}}}}]}))
    assert read_focus(a, m) == (True, "Work")


# --------------------------------------------------------------------------- queue


def alert(key, kind="market", priority="normal", expires=None, created=0.0):
    return Alert(key, kind, key, f"say {key}", priority, created=created, expires=expires)


def test_queue_priority_dedupe_expiry_persistence(store, clock):
    q = AlertQueue(store, clock)
    assert q.add(alert("a", priority="normal", created=1)) and not q.add(alert("a"))
    q.add(alert("b", priority="urgent", created=2))
    q.add(alert("c", priority="high", created=3))
    q.add(alert("gone", expires=clock() - 1))
    assert [x.key for x in q.pending()] == ["b", "c", "a"]
    assert [i["id"] for i in q.orb_items()] == ["b", "c", "a"]
    q2 = AlertQueue(store, clock)  # survives a restart
    assert [x.key for x in q2.pending()] == ["b", "c", "a"]
    taken = q2.take(2)
    assert [x.key for x in taken] == ["b", "c"] and len(q2) == 1
    q2.restore(taken)
    assert len(q2) == 3 and q2.clear() == 3 and len(AlertQueue(store, clock)) == 0


def test_queue_is_bounded(store, clock):
    q = AlertQueue(store, clock)
    for i in range(40):
        q.add(alert(f"n{i}", created=i))
    q.add(alert("vip", priority="urgent", created=99))
    assert len(q) <= 25 and "vip" in {a.key for a in q.pending()}


# --------------------------------------------------------------------------- engine


class FakeRule:
    def __init__(self, kind, alerts):
        self.kind, self.alerts, self.calls = kind, alerts, 0

    async def check(self, env):
        self.calls += 1
        return list(self.alerts)


class Speaker:
    def __init__(self):
        self.said, self.ok, self.free = [], True, True
        self.orb = []

    async def announce(self, text, *, earcon=None, remember=True, follow_up=True):
        if not self.ok:
            return False
        self.said.append((text, follow_up))
        return True

    async def orb_update(self, items):
        self.orb.append([i["id"] for i in items])


def make_engine(env, store, clock, rules, presence=None, **kw):
    sp = Speaker()
    q = AlertQueue(store, clock)
    eng = AlertEngine(env.cfg, env, q, presence or Presence(focus_reader=lambda: (False, ""), clock=clock),
                      announce=sp.announce, is_free=lambda: sp.free, orb_update=sp.orb_update,
                      rules=rules, now=clock.dt, clock=clock, **kw)
    return eng, sp


async def test_engine_delivers_once_and_dedupes(env, store, clock):
    rule = FakeRule("market", [alert("m1")])
    eng, sp = make_engine(env, store, clock, [rule])
    await eng.tick()
    assert sp.said == [("say m1", False)]
    clock.advance(5)
    await eng.poll()
    await eng.flush()
    assert len(sp.said) == 1  # same key never repeats
    assert sp.orb[-1] == []


async def test_engine_holds_during_call_then_batches(env, store, clock):
    presence = Presence(focus_reader=lambda: (False, ""), clock=clock)
    presence.on_device_usage(True, False, "zoom.us")
    rules = [FakeRule("meeting", [alert("x", "meeting", "high", created=1)]),
             FakeRule("vip", [Alert("v", "vip", "Priya", "Priya messaged you.", "urgent", created=2, follow_up=True,
                                    payload={"offer": {"type": "reply", "item_id": "9"}})])]
    eng, sp = make_engine(env, store, clock, rules, presence)
    await eng.tick()
    assert sp.said == [] and len(eng.queue) == 2
    assert sp.orb[-1] == ["v", "x"]  # urgent first on the orb
    clock.advance(10)
    presence.on_device_usage(False, False, "")
    await eng.on_idle()
    assert len(sp.said) == 1
    text, follow_up = sp.said[0]
    assert text.startswith("Sir, a few things came up while you were busy.") and text.index("Priya") < text.index("say x")
    assert follow_up is True
    assert env.ctx.state["alert_offer"]["item_id"] == "9"
    assert len(eng.queue) == 0


async def test_engine_force_flush_ignores_presence(env, store, clock):
    presence = Presence(focus_reader=lambda: (True, "Work"), clock=clock, focus_cache_s=0)
    eng, sp = make_engine(env, store, clock, [FakeRule("market", [alert("m")])], presence)
    await eng.tick()
    assert sp.said == []
    assert await eng.flush(force=True) and sp.said


async def test_engine_waits_when_busy_and_restores_on_failure(env, store, clock):
    eng, sp = make_engine(env, store, clock, [FakeRule("market", [alert("m")])])
    sp.free = False
    await eng.tick()
    assert sp.said == [] and len(eng.queue) == 1
    sp.free, sp.ok = True, False
    assert not await eng.flush() and len(eng.queue) == 1
    sp.ok = True
    assert await eng.flush() and len(eng.queue) == 0


async def test_engine_quiet_hours_hold_everything(env, store, clock):
    clock.t = at(4, 45).timestamp()
    eng, sp = make_engine(env, store, clock, [FakeRule("vip", [alert("v", "vip", "urgent")])])
    await eng.tick()
    assert sp.said == [] and len(eng.queue) == 1
    clock.t = at(9, 5).timestamp()
    await eng.flush()
    assert len(sp.said) == 1


async def test_engine_expired_alerts_are_dropped_not_spoken(env, store, clock):
    a = alert("m", "meeting", expires=clock() + 120)
    eng, sp = make_engine(env, store, clock, [FakeRule("meeting", [a])], Presence(focus_reader=lambda: (True, ""), clock=clock, focus_cache_s=0))
    await eng.tick()
    clock.advance(10)
    await eng.flush(force=False)
    assert sp.said == [] and len(eng.queue) == 0


async def test_engine_kind_cooldown_throttles_same_kind(env, store, clock):
    env.cfg.alerts.cooldown_minutes = {"news": 30}
    rule = FakeRule("news", [alert("n1", "news")])
    eng, sp = make_engine(env, store, clock, [rule])
    await eng.tick()
    rule.alerts = [alert("n2", "news")]
    clock.advance(10)
    await eng.poll()
    await eng.flush()
    assert len(sp.said) == 1  # throttled
    clock.advance(25)
    await eng.poll()
    await eng.flush()
    assert len(sp.said) == 2


async def test_engine_survives_failing_rule_and_marks_seen_ids(env, store, clock):
    class Boom:
        kind = "rain"

        async def check(self, env):
            raise RuntimeError("down")

    a = Alert("news:1", "news", "t", "s", payload={"seen_ids": ["h1", "h2"], "seen_kind": "news_alert"})
    eng, sp = make_engine(env, store, clock, [Boom(), FakeRule("news", [a])])
    await eng.tick()
    assert len(sp.said) == 1 and store.seen("news_alert", "h2")


async def test_engine_pause_and_resume(env, store, clock):
    rule = FakeRule("market", [alert("m")])
    eng, sp = make_engine(env, store, clock, [rule])
    await eng.on_pause()
    await eng.tick()
    assert rule.calls == 0 and sp.said == []
    await eng.on_resume()
    assert rule.calls == 1 and len(sp.said) == 1


def test_compose_batch_single_and_many(clock):
    assert compose_batch([alert("a")], clock(), held=False) == "say a"
    assert compose_batch([alert("a"), alert("b")], clock(), held=False) == "Sir, a few things. say a say b"


# ---------------------------------------------------------------------------- tools


async def test_alert_tools_and_router(env, store, clock):
    presence = Presence(focus_reader=lambda: (False, ""), clock=clock)
    eng, sp = make_engine(env, store, clock, [], presence)
    ctx = ToolContext(cfg=Config(), services={"alerts": eng, "presence": presence})
    reg = ToolRegistry()
    register(reg)
    assert await reg.get("read_alerts").handler(ctx, {}) == "Nothing outstanding, sir."
    eng.queue.add(alert("m"))
    assert await reg.get("read_alerts").handler(ctx, {}) == "say m" and len(eng.queue) == 0
    eng.queue.add(alert("z"))
    assert await reg.get("clear_alerts").handler(ctx, {}) == "Cleared, sir."
    assert "hour" in await reg.get("snooze_alerts").handler(ctx, {"minutes": 60})
    assert presence.state() == "snoozed"
    assert await reg.get("snooze_alerts").handler(ctx, {"minutes": 0}) == "Alerts resumed, sir."
    assert presence.state() == "free"

    router = IntentRouter.load()
    assert router.match("any alerts").tool == "read_alerts"
    c = router.match("Snooze alerts for 30 minutes")
    assert (c.tool, c.args) == ("snooze_alerts", {"minutes": "30"})
    c = router.match("don't disturb me for 2 hours")
    assert (c.tool, c.args) == ("snooze_alerts", {"minutes": "120"})
    assert router.match("resume alerts").args == {"minutes": "0"}


def test_screen_sharing_holds_alerts():
    from jarvis_core.policy.presence import Presence

    shared = {"on": False}
    p = Presence(focus_reader=lambda: (False, ""), clock=lambda: 1000.0 + len(shared) * 0, sharing_reader=lambda: shared["on"])
    assert p.can_speak() and p.state() == "free"
    shared["on"] = True
    p._sharing_at = -1e9  # past the cache
    assert p.state() == "sharing" and not p.can_speak()
