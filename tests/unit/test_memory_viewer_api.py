"""The Shell's Memory window: the daemon-side API and its IPC wiring."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from jarvis_core.interfaces import Fact
from jarvis_core.ipc import protocol as P
from jarvis_core.ipc.frontend import IPCFrontend
from jarvis_core.memory import viewer_api as V
from jarvis_core.memory.store import SQLiteMemory


@pytest.fixture
def mem(tmp_path):
    m = SQLiteMemory(tmp_path / "m.db")
    m.add(Fact(kind="person", text="The user's brother is Arjun", meta={"alias": "brother", "name": "Arjun"}))
    m.add(Fact(kind="preference", text="I prefer tea"))
    m.add(Fact(kind="fact", text="I live in Pune"))
    m.add(Fact(kind="loop", text="Follow up with the dentist"))
    m.add(Fact(kind="episode", text="[Mon] You said: hi | I replied: hello"))
    m.add(Fact(kind="style", text="short and warm", meta={"app": "whatsapp"}))
    yield m
    m.close()


# ------------------------------------------------------------------ list


def test_list_groups_in_order_and_hides_episodes_and_styles(mem):
    r = V.list_facts(mem)
    assert r["ok"] and r["total"] == 4
    assert [g["title"] for g in r["groups"]] == ["People", "Preferences", "Facts", "Loops"]
    assert all(set(i) == {"id", "text", "date"} for g in r["groups"] for i in g["items"])
    assert "hello" not in json.dumps(r) and "warm" not in json.dumps(r)


def test_empty_groups_are_left_out(tmp_path):
    m = SQLiteMemory(tmp_path / "e.db")
    m.add(Fact(kind="fact", text="I live in Pune"))
    assert [g["kind"] for g in V.list_facts(m)["groups"]] == ["fact"]
    assert V.list_facts(SQLiteMemory(tmp_path / "empty.db")) == {"ok": True, "groups": [], "total": 0, "query": ""}


def test_search_narrows_the_list(mem):
    r = V.list_facts(mem, "brother arjun")
    assert r["total"] == 1 and r["groups"][0]["items"][0]["text"] == "The user's brother is Arjun"
    assert V.list_facts(mem, "quantum")["total"] == 0


def test_secrets_never_reach_the_window(mem):
    mem.add(Fact(kind="fact", text="my password: hunter2hunter and card 4111 1111 1111 1111"))
    text = json.dumps(V.list_facts(mem))
    assert "hunter2hunter" not in text and "4111" not in text and "redacted" in text


# ------------------------------------------------------------------ forget / edit / clear


def test_forget_one(mem):
    fid = V.list_facts(mem, "tea")["groups"][0]["items"][0]["id"]
    assert V.forget(mem, fid) == {"ok": True, "id": fid}
    assert V.list_facts(mem, "tea")["total"] == 0 and V.list_facts(mem)["total"] == 3


def test_forget_a_missing_id_is_a_plain_error(mem):
    r = V.forget(mem, "nope")
    assert not r["ok"] and "already gone" in r["error"]
    assert not V.forget(mem, "")["ok"]


def test_edit_changes_the_wording_and_keeps_the_kind(mem):
    item = V.list_facts(mem, "pune")["groups"][0]["items"][0]
    assert V.edit(mem, item["id"], "  I live in   Delhi ")["ok"]
    f = mem.get(item["id"])
    assert f.text == "I live in Delhi" and f.kind == "fact"


def test_editing_a_person_re_derives_alias_and_name(mem):
    pid = V.list_facts(mem, "arjun")["groups"][0]["items"][0]["id"]
    assert V.edit(mem, pid, "my brother is Rahul")["ok"]
    f = mem.get(pid)
    assert f.kind == "person" and f.meta["name"] == "Rahul"


@pytest.mark.parametrize("text,fragment", [("", "empty"), ("   ", "empty"), ("x" * 400, "too long")])
def test_bad_edits_are_refused_and_nothing_changes(mem, text, fragment):
    item = V.list_facts(mem, "pune")["groups"][0]["items"][0]
    r = V.edit(mem, item["id"], text)
    assert not r["ok"] and fragment in r["error"] and mem.get(item["id"]).text == "I live in Pune"


def test_edit_a_missing_memory(mem):
    assert not V.edit(mem, "gone", "hello")["ok"]


def test_clear_forgets_everything(mem):
    assert V.clear(mem) == {"ok": True, "forgotten": 6}
    assert V.list_facts(mem)["total"] == 0 and mem.list() == []


# ------------------------------------------------------------------ dispatch


def test_handle_routes_and_never_raises(mem):
    assert V.handle(mem, "memory.list", {"query": "tea"})["total"] == 1
    assert not V.handle(mem, "memory.bogus", {})["ok"]
    assert V.handle(mem, "memory.edit", {"id": None, "text": None})["ok"] is False

    class Broken:
        def get(self, _):
            raise RuntimeError("disk on fire")

    r = V.handle(Broken(), "memory.forget", {"id": "x"})
    assert r["ok"] is False and "RuntimeError" in r["error"]


# ------------------------------------------------------------------ over the socket


async def _client(info):
    ws = await connect(f"ws://127.0.0.1:{info['port']}/ipc")
    await ws.send(P.Control(P.HELLO, {"token": info["token"], "shell_version": "t", "permissions": {}}).encode())
    return ws


async def _recv(ws, type_, timeout=3):
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(raw, bytes):
            continue
        c = P.Control.decode(raw)
        if c.type == type_:
            return c


@pytest.fixture
async def rig(tmp_path, mem):
    fe = IPCFrontend(str(tmp_path / "ipc.json"), shell_lost_timeout_s=60)
    db = mem.path
    fe.memory_factory = lambda: SQLiteMemory(db)
    await fe.start()
    yield fe, json.loads((tmp_path / "ipc.json").read_text())
    await fe.stop()


async def ask(ws, op, **args):
    req = P.Control(P.MEMORY_REQUEST, {"op": op, **args}, id=f"r-{op}")
    await ws.send(req.encode())
    res = await _recv(ws, P.MEMORY_RESULT)
    assert res.id == req.id  # the answer comes back on the same id
    return res.data


async def test_the_window_can_list_edit_forget_and_clear_over_ipc(rig):
    _, info = rig
    ws = await _client(info)
    try:
        await _recv(ws, P.WELCOME)
        listing = await ask(ws, "memory.list")
        assert listing["ok"] and listing["total"] == 4
        tea = (await ask(ws, "memory.list", query="tea"))["groups"][0]["items"][0]["id"]
        assert (await ask(ws, "memory.edit", id=tea, text="I prefer coffee"))["ok"]
        assert (await ask(ws, "memory.list", query="coffee"))["total"] == 1
        assert (await ask(ws, "memory.forget", id=tea))["ok"]
        assert (await ask(ws, "memory.list"))["total"] == 3
        assert (await ask(ws, "memory.clear"))["ok"]
        assert (await ask(ws, "memory.list"))["total"] == 0
        assert not (await ask(ws, "memory.what"))["ok"]
    finally:
        await ws.close()


async def test_a_broken_memory_gives_the_window_an_error_not_silence(rig):
    fe, info = rig

    def boom():
        raise OSError("locked")

    fe.memory_factory = boom
    ws = await _client(info)
    try:
        await _recv(ws, P.WELCOME)
        r = await ask(ws, "memory.list")
        assert r["ok"] is False and "memory" in r["error"].lower()
    finally:
        await ws.close()
