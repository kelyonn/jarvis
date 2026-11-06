from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from jarvis_core import doctor, paths, secrets, setup_wizard
from jarvis_core.config import load_config, save_config
from jarvis_core.housekeeping import TranscriptLog
from jarvis_core.interfaces import Done, TextDelta, ToolCallEvent


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: h))
    return h


class FakeNim:
    def __init__(self, healthy=True, models=None, calls_tool=True):
        self.healthy, self.models, self.calls_tool = healthy, models if models is not None else [], calls_tool

    def health(self):
        return self.healthy

    def list_models(self):
        return list(self.models)

    async def stream_chat(self, messages, tools=None, **kw):
        if self.calls_tool:
            yield ToolCallEvent("1", "ping", {"word": "hello"})
            yield Done("tool_calls")
        else:
            yield TextDelta("hello")
            yield Done()


def by_name(checks):
    return {c.name: c for c in checks}


# ------------------------------------------------------------------------------------- doctor


async def test_nim_checks(monkeypatch):
    cfg = load_config()
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: "nvapi-test")
    good = by_name(await doctor.check_nim(cfg, FakeNim(models=[cfg.llm.chat_model, cfg.llm.fast_model, cfg.llm.vision_model])))
    assert good["NIM reachable"].status == doctor.OK and good["tool calling"].status == doctor.OK
    assert not [c for c in good if c.endswith("model")]
    partial = by_name(await doctor.check_nim(cfg, FakeNim(models=["some/other-model"])))
    assert partial["fast model"].status == doctor.WARN and partial["vision model"].status == doctor.WARN
    down = await doctor.check_nim(cfg, FakeNim(healthy=False))
    assert [c.status for c in down] == [doctor.FAIL]
    no_tools = by_name(await doctor.check_nim(cfg, FakeNim(calls_tool=False)))
    assert no_tools["tool calling"].status == doctor.FAIL
    assert "tool calling" not in by_name(await doctor.check_nim(cfg, FakeNim(), probe_tools=False))
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: None)
    assert await doctor.check_nim(cfg) == []


def test_key_and_config_checks(monkeypatch, jarvis_home):
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: None)
    assert doctor.check_nim_key().status == doctor.FAIL
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: "k")
    assert doctor.check_nim_key().status == doctor.OK
    c, cfg = doctor.check_config()
    assert c.status == doctor.OK and cfg is not None
    paths.config_file().write_text("[user]\nquiet_hours = ['25:00', '07:00']\n")
    c, cfg = doctor.check_config()
    assert c.status == doctor.FAIL and cfg is None and "jarvisd config edit" in c.fix


def test_voice_stack_and_tools(monkeypatch):
    cfg = load_config()
    monkeypatch.setattr(doctor, "_spec", lambda m: m in {"openwakeword", "mlx_whisper"})
    monkeypatch.setattr(doctor, "hf_cached", lambda repo: False)
    c = by_name(doctor.check_voice_stack(cfg))
    assert c["wake word"].status == doctor.OK and c["Kokoro voice"].status == doctor.WARN
    assert c["STT model"].status == doctor.WARN and "--fix" in c["STT model"].fix
    monkeypatch.setattr(doctor, "hf_cached", lambda repo: True)
    assert by_name(doctor.check_voice_stack(cfg))["STT model"].status == doctor.OK
    monkeypatch.setattr("shutil.which", lambda t: "/opt/homebrew/bin/blueutil" if t == "blueutil" else None)
    b = by_name(doctor.check_brew_tools())
    assert b["blueutil"].status == doctor.OK and b["brightness"].fix == "brew install brightness"


def test_shell_checks(home, jarvis_home):
    first = by_name(doctor.check_shell_app())
    assert first["Jarvis.app"].status == doctor.WARN and "no report" in first["shell permissions"].detail
    (home / "Applications").mkdir()
    (home / "Applications" / "Jarvis.app").mkdir()
    status = paths.run_dir() / "shell_status.json"
    status.write_text(json.dumps({"ts": time.time(), "permissions": {"mic": True, "accessibility": False, "screen": False}}))
    c = by_name(doctor.check_shell_app())
    assert c["Jarvis.app"].status == doctor.OK
    assert "Accessibility, Screen Recording" in c["shell permissions"].detail and c["shell permissions"].status == doctor.WARN
    status.write_text(json.dumps({"ts": time.time(), "permissions": {"mic": True, "accessibility": True, "screen": True}}))
    assert by_name(doctor.check_shell_app())["shell permissions"].status == doctor.OK
    status.write_text(json.dumps({"ts": time.time() - 9 * 86400, "permissions": {"mic": True}}))
    assert "days old" in by_name(doctor.check_shell_app())["shell permissions"].detail
    assert by_name(doctor.check_shell_app())["daemon"].status == doctor.WARN
    paths.ipc_file().write_text(json.dumps({"pid": 999_999}))  # a daemon that died without cleaning up
    stale = by_name(doctor.check_shell_app())["daemon"]
    assert stale.status == doctor.WARN and "stale" in stale.detail
    paths.ipc_file().write_text(json.dumps({"pid": os.getpid()}))
    assert by_name(doctor.check_shell_app())["daemon"].status == doctor.OK


def test_full_disk_guidance(monkeypatch, home):
    cfg = load_config()
    monkeypatch.setattr("jarvis_core.policy.presence.read_focus", lambda *a, **k: (None, ""))
    c = by_name(doctor.check_full_disk(cfg))
    assert c["Focus detection"].status == doctor.WARN and "Full Disk Access" in c["Focus detection"].fix
    assert c["WhatsApp"].status == doctor.WARN  # no database under the fake home
    monkeypatch.setattr("jarvis_core.policy.presence.read_focus", lambda *a, **k: (False, ""))
    assert by_name(doctor.check_full_disk(cfg))["Focus detection"].status == doctor.OK


def test_gmail_scope_checks(monkeypatch):
    import jarvis_core.adapters.services as br

    cfg = load_config()

    class S:
        def __init__(self, ok=True, scope="", valid=True):
            self.ok, self.scope, self._valid = ok, scope, valid

        def connected(self):
            return self.ok

        def valid(self):
            return self._valid

        def scopes(self):
            return self.scope

    def with_session(s):
        monkeypatch.setattr(br, "GoogleSession", lambda *_: s)
        return doctor.check_gmail(cfg)

    assert with_session(S(False)).detail == "not signed in"
    revoked = with_session(S(scope="https://www.googleapis.com/auth/gmail.modify", valid=False))
    assert revoked.status == doctor.WARN and "revoked" in revoked.detail and "--reauth" in revoked.fix
    assert with_session(S(scope="https://www.googleapis.com/auth/gmail.readonly")).status == doctor.WARN
    assert with_session(S(scope="https://www.googleapis.com/auth/gmail.modify")).status == doctor.OK
    risky = with_session(S(scope="https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send"))
    assert risky.status == doctor.WARN and "send scope" in risky.detail
    cfg.messaging.apps = ["whatsapp"]
    assert doctor.check_gmail(cfg) is None


def test_data_file_health(jarvis_home):
    assert all(c.status == doctor.OK for c in doctor.check_data_files())
    paths.memory_db().write_bytes(b"this is not a database at all" * 50)
    c = by_name(doctor.check_data_files())["memory.db"]
    assert c.status == doctor.FAIL and "move" in c.fix


async def test_run_doctor_exit_code_and_output(monkeypatch, home, jarvis_home):
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: None)
    lines: list[str] = []
    assert await doctor.run_doctor(network=False, out=lines.append) == 1  # no key is broken
    text = "\n".join(lines)
    assert "✗ NIM API key" in text and "jarvisd set-key" in text and "broken" in lines[-1]
    monkeypatch.setattr(secrets, "get_secret", lambda *a, **k: "k")
    lines.clear()
    assert await doctor.run_doctor(network=False, out=lines.append) == 0  # warnings alone are not failures
    paths.config_file().write_text("not toml [[[")
    lines.clear()
    assert await doctor.run_doctor(network=False, out=lines.append) == 1


# ------------------------------------------------------------------------------ housekeeping


async def test_transcripts_written_pruned_and_redacted(jarvis_home):
    now = time.time()
    t = TranscriptLog(7, clock=lambda: now)
    await t.on_turn_done("my key is nvapi-ABCDEFGHIJKLMNOP1234", "ok sir")
    files = list(t.dir.glob("*.jsonl"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o077 == 0
    rec = json.loads(files[0].read_text())
    assert "nvapi" not in rec["user"] and rec["jarvis"] == "ok sir"
    old = t.dir / "2020-01-01.jsonl"
    old.write_text("{}\n")
    os.utime(old, (now - 30 * 86400,) * 2)
    assert t.prune() == 1 and not old.exists() and files[0].exists()
    off = TranscriptLog(0, directory=t.dir)
    await off.on_turn_done("hi", "hello")
    assert off.prune() == 1 and not list(t.dir.glob("*.jsonl"))  # retention 0 also clears what exists


# --------------------------------------------------------------------------------------- wizard


def test_wizard_helpers():
    assert setup_wizard.threshold_from_peaks([0.9, 0.8, 0.95]) == 0.64
    assert setup_wizard.threshold_from_peaks([0.2]) == 0.35 and setup_wizard.threshold_from_peaks([1.0]) == 0.7
    assert setup_wizard.threshold_from_peaks([]) == 0.5
    tr = setup_wizard.PeakTracker()
    scores = [0.0, 0.1, 0.5, 0.9, 0.6, 0.05, 0.0, 0.7, 0.04]
    done = [tr.feed(s) for s in scores]
    assert tr.peaks == [0.9, 0.7] and done.count(True) == 2
    assert setup_wizard.parse_times("1pm, 7:30 pm 21", []) == ["13:00", "19:30", "21:00"]
    assert setup_wizard.parse_times("12am", []) == ["00:00"] and setup_wizard.parse_times("", ["09:00"]) == ["09:00"]
    with pytest.raises(ValueError):
        setup_wizard.parse_times("noon", [])
    assert setup_wizard.valid_timezone("Asia/Kolkata") and not setup_wizard.valid_timezone("Mars/Base")


async def test_non_interactive_setup(monkeypatch, jarvis_home, capsys, home):
    stored = {}
    monkeypatch.setenv("NIM_API_KEY", "nvapi-xyz")
    monkeypatch.setattr(secrets, "get_secret", lambda name, env_fallback=True: stored.get(name))
    monkeypatch.setattr(secrets, "set_secret", lambda n, v: stored.__setitem__(n, v))
    code = await setup_wizard.run_setup(non_interactive=True)
    assert code == 0 and stored == {"NIM_API_KEY": "nvapi-xyz"} and paths.config_file().exists()
    assert "Jarvis doctor" in capsys.readouterr().out


async def test_interactive_wizard_end_to_end(monkeypatch, jarvis_home, home):
    stored: dict[str, str] = {}
    monkeypatch.setattr(secrets, "get_secret", lambda name, env_fallback=True: stored.get(name))
    monkeypatch.setattr(secrets, "set_secret", lambda n, v: stored.__setitem__(n, v))
    script: list[str] = []
    out: list[str] = []
    opened: list[str] = []
    spoken: list[str] = []

    def ask(prompt: str) -> str:
        script.append(prompt)
        p = prompt.lower()
        if "call you" in p:
            return "Kelyon"
        if p.startswith("timezone"):
            return "Mars/Base" if script.count(prompt) == 1 else "Asia/Kolkata"
        if "city" in p:
            return "Bengaluru"
        if "open that pane" in p:
            return "y" if not opened else "n"  # only the first pane is opened
        if "press enter" in p or "switch jarvis on" in p:
            return ""
        if "hey jarvis" in p:
            return "n"
        if "sign in to google" in p:
            return "n"
        if "vip names" in p:
            return "Rahul, Meera"
        if "brief times" in p:
            return "9am, 6:30pm"
        if "quiet hours" in p:
            return "22:00 to 6:30"
        if "shortcuts name" in p:
            return "Work Focus"
        if "work mode" in p:
            return "Cursor, Safari"
        if "study mode" in p:
            return ""
        if "run a briefing" in p:
            return "n"
        return ""

    class FakeLLM:
        def health(self):
            return True

        def list_models(self):
            return ["meta/llama-3.3-70b-instruct"]

    io = setup_wizard.IO(ask=ask, secret=lambda p: "nvapi-secret", out=out.append, speak=spoken.append,
                         open_pane=opened.append, open_app=lambda p: None)
    code = await setup_wizard.run_setup(io=io, llm=FakeLLM())
    assert code == 0
    cfg = load_config()
    assert (cfg.user.name, cfg.user.timezone, cfg.devices.location_city) == ("Kelyon", "Asia/Kolkata", "Bengaluru")
    assert cfg.briefing.slots == ["09:00", "18:30"] and cfg.user.quiet_hours == ["22:00", "06:30"]
    assert cfg.routines["work"].apps == ["Cursor", "Safari"] and cfg.routines["work"].focus == "Work Focus"
    assert "study" not in cfg.routines
    assert stored["NIM_API_KEY"] == "nvapi-secret"
    assert opened == ["Privacy_Microphone"]
    assert any("isn't a timezone" in line for line in out)
    assert any("NIM answered" in line for line in out) and any("isn't in your NIM catalogue" in line for line in out)
    assert (paths.home() / "vip.json").exists() and set(json.loads((paths.home() / "vip.json").read_text())) == {"Rahul", "Meera"}
    assert spoken[0].startswith("Jarvis setup") and spoken[-1].startswith("Setup complete")
    assert "secret" not in "\n".join(out)  # the key is never echoed


def test_config_roundtrip_keeps_wizard_changes(jarvis_home):
    cfg = load_config()
    cfg.activation.wake_threshold = 0.42
    save_config(cfg)
    assert load_config().activation.wake_threshold == 0.42


class ProbingNim(FakeNim):
    def __init__(self, dead=()):
        super().__init__(models=[])
        self.dead = set(dead)

    async def probe(self, model):
        return (False, "NIM returned HTTP 404") if model in self.dead else (True, "answers")


async def test_models_are_probed_not_just_listed():
    cfg = load_config()
    ok = by_name(await doctor.check_models(cfg, ProbingNim()))
    assert all(ok[f"{r} model"].status == doctor.OK for r in ("chat", "fast", "vision"))
    gone = by_name(await doctor.check_models(cfg, ProbingNim(dead={cfg.llm.chat_model})))
    assert gone["chat model"].status == doctor.WARN and "falling back to" in gone["chat model"].detail
    cfg.llm.fallback_models = []
    assert by_name(await doctor.check_models(cfg, ProbingNim(dead={cfg.llm.chat_model})))["chat model"].status == doctor.FAIL


def test_play_music_routing():
    from jarvis_core.router import IntentRouter

    r = IntentRouter.load()
    m = r.match("Play Mr. Brightside by the Killers on Spotify.")
    assert m and m.tool == "play_music"
    assert m.args == {"query": "mr brightside by the killers", "app": "spotify"}
    m = r.match("open spotify and play despacito")
    assert m and m.tool == "play_music" and m.args["query"] == "despacito"
    assert r.match("play music").tool == "media_control"  # bare "play" still toggles
    assert r.match("play a youtube video") is None
    assert r.match("open spotify").tool == "open_app"


async def test_direct_reply_drops_untrusted_wrapper():
    import asyncio  # noqa: F401
    import re

    from jarvis_core.brain.persona import wrap_untrusted

    wrapped = wrap_untrusted("Your calendar is clear.", "get_calendar")
    assert "<untrusted" in wrapped
    assert re.sub(r"</?untrusted[^>]*>\s*", "", wrapped).strip() == "Your calendar is clear."


def test_playlist_routing_prefers_own_playlists():
    from jarvis_core.router import IntentRouter

    r = IntentRouter.load()
    for text, name in [
        ("open Spotify and play my radiohead playlist", "radiohead"),
        ("play my workout playlist on spotify", "workout"),
        ("play the main character energy playlist from my folder", "main character energy"),
        ("play playlist gym hype", "gym hype"),
    ]:
        m = r.match(text)
        assert m and m.tool == "play_my_playlist" and m.args["name"] == name, text
    assert r.match("shuffle my stargazing playlist").args["shuffle"] == "true"
    assert r.match("play my liked songs").tool == "play_liked_songs"
    assert r.match("play radiohead").tool == "play_music"


def test_audit_hashes_message_content(tmp_path):
    from jarvis_core.policy.audit import AuditLog

    log = AuditLog(tmp_path / "audit.log")
    log.write("tool", tool="draft_message", args={"to": "Mom", "text": "I will be home late tonight, love you"},
              result="Draft ready: I will be home late tonight, love you", ok=True)
    log.write("tool", tool="open_app", args={"name": "Safari"}, result="Opened Safari.", ok=True)
    raw = (tmp_path / "audit.log").read_text()
    assert "home late" not in raw and "sha256:" in raw
    assert "Opened Safari." in raw  # ordinary tools stay readable


def test_setup_wizard_vip_pick_by_number():
    from jarvis_core.setup_wizard import pick_names

    chats = ["Mom", "Rahul Sharma", "Priya"]
    assert pick_names("1, 3, Dad", chats) == ["Mom", "Priya", "Dad"]
    assert pick_names("2,2,9", chats) == ["Rahul Sharma"]
    assert pick_names("", chats) == []
