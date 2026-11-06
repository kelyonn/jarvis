from __future__ import annotations

import asyncio

import pytest
from fakes import Rig

from jarvis_core import runtime as rtmod
from jarvis_core.alerts import engine as engine_mod
from jarvis_core.alerts.models import Alert
from jarvis_core.briefing.env import Env
from jarvis_core.briefing.store import Store
from jarvis_core.state import State


async def wait_for(pred, timeout=3.0):
    end = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


@pytest.fixture
async def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(engine_mod, "default_rules", lambda: [])
    rig = Rig()
    rig.cfg.user.quiet_hours = ["03:00", "03:01"]  # keep the tests independent of the wall clock
    store = Store(tmp_path / "state.db")
    env = Env(cfg=rig.cfg, http=Env.make_client(), store=store, ctx=rig.assistant.ctx)
    rt = rtmod.Runtime(rig.cfg, rig.daemon, rig.frontend, rig.assistant)
    rt.extras.update(store=store, env=env)
    rtmod._attach_alerts(rt)
    await rig.start()
    yield rig, rt
    await rig.stop()
    await env.http.aclose()
    store.close()


def alert(key="a1"):
    return Alert(key, "market", "Nifty -2%", "Sir, Nifty has dropped 2 percent.", "normal")


async def test_alert_spoken_when_idle_and_orb_mirrors_queue(wired):
    rig, rt = wired
    engine = rt.extras["alerts"]
    engine.queue.add(alert())
    await engine.on_idle()
    await wait_for(lambda: rig.tts.spoken)
    assert rig.tts.spoken == ["Sir, Nifty has dropped two percent."]  # numbers normalised for TTS
    assert "alert" in rig.frontend.earcons
    assert rig.assistant.history.messages()[-1]["content"] == "Sir, Nifty has dropped 2 percent."


async def test_call_holds_alert_until_it_ends(wired):
    rig, rt = wired
    engine = rt.extras["alerts"]
    rig.daemon.on_device_usage(True, False, "zoom.us")  # user joins a call
    engine.queue.add(alert())
    await engine.on_idle()
    await asyncio.sleep(0.05)
    assert rig.tts.spoken == [] and len(engine.queue) == 1
    assert rt.extras["gate"]() is False  # briefings wait too
    rig.daemon.on_device_usage(False, False, "")  # call ends -> idle hooks -> flush
    await wait_for(lambda: rig.tts.spoken)
    assert len(engine.queue) == 0 and rt.extras["gate"]() is True


async def test_orb_tap_delivers_even_during_focus(wired):
    rig, rt = wired
    presence = rt.extras["presence"]
    presence._read_focus = lambda: (True, "Work")
    presence._focus_cache_s = 0
    engine = rt.extras["alerts"]
    engine.queue.add(alert())
    await engine.flush()
    assert rig.tts.spoken == []
    rig.daemon.on_orb_click("open_queue")
    await wait_for(lambda: rig.tts.spoken)
    await rig.wait_state(State.FOLLOW_UP, State.IDLE)
