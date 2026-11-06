from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from jarvis_core.frontend import FrontendError
from jarvis_core.ipc import protocol as P
from jarvis_core.ipc.frontend import IPCFrontend


class Events:
    def __init__(self):
        self.audio, self.hotkeys, self.lifecycle, self.usage, self.clicks = [], [], [], [], []
        self.connected = []
        self.disconnected = 0

    def on_audio(self, f): self.audio.append(f)
    def on_hotkey(self, a): self.hotkeys.append(a)
    def on_lifecycle(self, e): self.lifecycle.append(e)
    def on_device_usage(self, m, c, h): self.usage.append((m, c, h))
    def on_orb_click(self, a): self.clicks.append(a)
    def on_connected(self, i): self.connected.append(i)
    def on_disconnected(self): self.disconnected += 1


@pytest.fixture
async def rig(tmp_path):
    fe = IPCFrontend(str(tmp_path / "ipc.json"), shell_lost_timeout_s=60)
    fe.events = Events()
    await fe.start()
    info = json.loads((tmp_path / "ipc.json").read_text())
    yield fe, info
    await fe.stop()


async def _client(info, token=None):
    ws = await connect(f"ws://127.0.0.1:{info['port']}/ipc")
    await ws.send(P.Control(P.HELLO, {"token": token or info["token"], "shell_version": "t",
                                       "permissions": {"mic": True, "ax": False}}).encode())
    return ws


async def _recv_ctl(ws, type_=None, timeout=2):
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(raw, bytes):
            continue
        c = P.Control.decode(raw)
        if type_ is None or c.type == type_:
            return c


async def _recv_bin(ws, timeout=2):
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(raw, bytes):
            return raw


async def test_ipc_file_is_private_and_handshake(rig, tmp_path):
    fe, info = rig
    assert (tmp_path / "ipc.json").stat().st_mode & 0o777 == 0o600
    ws = await _client(info)
    assert (await _recv_ctl(ws, P.WELCOME)).type == P.WELCOME
    await asyncio.sleep(0.05)
    assert fe.connected and fe.permissions == {"mic": True, "ax": False}
    assert fe.events.connected and "token" not in fe.events.connected[0]
    await ws.close()


async def test_bad_token_rejected(rig):
    fe, info = rig
    ws = await _client(info, token="nope")
    with pytest.raises(Exception):
        await asyncio.wait_for(ws.recv(), 2)
    assert not fe.connected


async def test_inbound_events_routed(rig):
    fe, info = rig
    ws = await _client(info)
    await _recv_ctl(ws, P.WELCOME)
    await ws.send(P.encode_mic_frame(b"\x01\x02" * 320))
    await ws.send(P.Control(P.HOTKEY, {"action": "down"}).encode())
    await ws.send(P.Control(P.LIFECYCLE, {"event": "will_sleep"}).encode())
    await ws.send(P.Control(P.LIFECYCLE, {"event": "bogus"}).encode())
    await ws.send(P.Control(P.DEVICE_USAGE, {"mic_in_use_by_other": True, "camera_in_use": False,
                                             "app_hint": "zoom.us"}).encode())
    await ws.send(P.Control(P.ORB_CLICK, {"action": "open_queue"}).encode())
    await asyncio.sleep(0.1)
    ev = fe.events
    assert ev.audio == [b"\x01\x02" * 320]
    assert ev.hotkeys == ["down"] and ev.lifecycle == ["will_sleep"]
    assert ev.usage == [(True, False, "zoom.us")] and ev.clicks == ["open_queue"]
    await ws.close()


async def test_playback_roundtrip_and_stop(rig):
    fe, info = rig
    ws = await _client(info)
    await _recv_ctl(ws, P.WELCOME)
    await asyncio.sleep(0.05)
    utt = "ab" * 16
    await fe.play_begin(utt, True)
    assert (await _recv_ctl(ws, P.PLAY_BEGIN)).data["utterance_id"] == utt
    await fe.play_chunk(utt, b"\x05\x00" * 100, 24000)
    frame = await _recv_bin(ws)
    kind, uid, payload = P.decode_binary(frame)
    assert kind == P.TTS_FRAME and uid == utt and payload == b"\x05\x00" * 100
    await fe.play_end(utt)
    await _recv_ctl(ws, P.PLAY_END)
    waiter = asyncio.create_task(fe.wait_playback_done(utt))
    await ws.send(P.Control(P.PLAYBACK, {"utterance_id": utt, "event": "finished"}).encode())
    assert await asyncio.wait_for(waiter, 2) == "finished"

    utt2 = "cd" * 16
    await fe.play_begin(utt2)
    waiter = asyncio.create_task(fe.wait_playback_done(utt2))
    await asyncio.sleep(0.02)
    await fe.stop_audio()
    assert await asyncio.wait_for(waiter, 2) == "interrupted"
    await _recv_ctl(ws, P.STOP_AUDIO)
    await ws.close()


async def test_resamples_non_24k_audio(rig):
    fe, info = rig
    ws = await _client(info)
    await _recv_ctl(ws, P.WELCOME)
    await asyncio.sleep(0.05)
    await fe.play_chunk("ee" * 16, b"\x10\x00" * 160, 16000)
    _, _, payload = P.decode_binary(await _recv_bin(ws))
    assert len(payload) == 240 * 2
    await ws.close()


async def test_request_response_and_errors(rig):
    fe, info = rig
    ws = await _client(info)
    await _recv_ctl(ws, P.WELCOME)
    await asyncio.sleep(0.05)

    async def answer(ok=True):
        req = await _recv_ctl(ws, P.AX_REQUEST)
        assert req.data["op"] == "list_windows"
        body = {"request_id": req.data["request_id"], "ok": ok}
        body.update({"data": {"windows": [1]}} if ok else {"error": "no permission"})
        await ws.send(P.Control(P.AX_RESULT, body).encode())

    t = asyncio.create_task(answer())
    assert await fe.request("ax.list_windows", {"app": "Safari"}) == {"windows": [1]}
    await t
    t = asyncio.create_task(answer(False))
    with pytest.raises(FrontendError, match="no permission"):
        await fe.request("ax.list_windows")
    await t
    with pytest.raises(FrontendError, match="timed out"):
        await fe.request("screenshot", timeout=0.1)
    with pytest.raises(ValueError):
        await fe.request("bogus.op")
    await ws.close()


async def test_disconnect_resolves_waiters_and_requests(rig):
    fe, info = rig
    ws = await _client(info)
    await _recv_ctl(ws, P.WELCOME)
    await asyncio.sleep(0.05)
    await fe.play_begin("11" * 16)
    w = asyncio.create_task(fe.wait_playback_done("11" * 16))
    r = asyncio.create_task(fe.request("location"))
    await asyncio.sleep(0.05)
    await ws.close()
    assert await asyncio.wait_for(w, 2) == "interrupted"
    with pytest.raises(FrontendError):
        await asyncio.wait_for(r, 2)
    assert not fe.connected and fe.events.disconnected == 1


async def test_no_shell_means_playback_does_not_stall(rig):
    fe, _ = rig
    await fe.play_begin("22" * 16)
    await fe.play_end("22" * 16)
    assert await asyncio.wait_for(fe.wait_playback_done("22" * 16), 1) == "finished"
    with pytest.raises(FrontendError):
        await fe.request("location")


async def test_state_replayed_on_reconnect(rig):
    fe, info = rig
    await fe.set_state("follow_up", "hi")
    await fe.orb_queue([{"id": "1", "title": "x", "priority": "normal"}])
    ws = await _client(info)
    st = await _recv_ctl(ws, P.STATE)
    assert st.data["name"] == "follow_up"
    q = await _recv_ctl(ws, P.ORB_QUEUE)
    assert q.data["items"][0]["id"] == "1"
    await ws.close()


async def test_shell_lost_timer(tmp_path):
    lost = []
    fe = IPCFrontend(str(tmp_path / "i.json"), shell_lost_timeout_s=0.2, on_shell_lost=lambda: lost.append(1))
    fe.events = Events()
    await fe.start()
    await asyncio.sleep(0.4)
    assert lost == [1]
    await fe.stop()


def test_protocol_codec():
    c = P.Control.decode(P.Control("x", {"a": 1}, "id1").encode())
    assert (c.type, c.data, c.id) == ("x", {"a": 1}, "id1")
    with pytest.raises(ValueError):
        P.Control.decode("[]")
    with pytest.raises(ValueError):
        P.decode_binary(b"\x09abc")
    assert P.split_op("ax.move") == (P.AX_REQUEST, "move")
    assert P.split_op("screenshot") == (P.SCREENSHOT_REQUEST, "")
    assert len(P.resample_pcm16(b"\x00\x01" * 100, 48000, 24000)) == 100
