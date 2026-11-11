"""``jarvisd doctor``: one screen that says what works, what doesn't, and how to fix it."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis_core import paths, secrets
from jarvis_core.config import Config, load_config

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✓", WARN: "!", FAIL: "✗"}
MIN_FREE_GB = 5.0
STATUS_STALE_S = 7 * 86400
SHELL_PERMS = {
    "mic": "Microphone", "accessibility": "Accessibility", "screen": "Screen Recording", "location": "Location",
}
HF_STT = "models--{}"  # e.g. models--mlx-community--whisper-large-v3-turbo


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    def line(self) -> str:
        out = f" {ICON[self.status]} {self.name:<22} {self.detail}"
        if self.fix and self.status != OK:
            out += f"\n   → {self.fix}"
        return out


def _spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def hf_cached(repo: str) -> bool:
    root = Path.home() / ".cache" / "huggingface" / "hub" / HF_STT.format(repo.replace("/", "--"))
    return (root / "snapshots").exists() and any((root / "snapshots").iterdir())


# --------------------------------------------------------------------------------------- sync checks


def check_config() -> tuple[Check, Config | None]:
    try:
        cfg = load_config()
    except Exception as e:
        return Check("config", FAIL, f"invalid: {str(e).splitlines()[0][:120]}", "jarvisd config edit"), None
    where = paths.config_file()
    return Check("config", OK, str(where) if where.exists() else "defaults (no config.toml yet; run jarvisd setup)"), cfg


def check_nim_key() -> Check:
    if secrets.get_secret("NIM_API_KEY"):
        return Check("NIM API key", OK, "found")
    return Check("NIM API key", FAIL, "not set", "jarvisd set-key   (get a key at build.nvidia.com)")


def check_voice_stack(cfg: Config) -> list[Check]:
    out: list[Check] = []
    pieces = [("wake word", "openwakeword", "hey Jarvis detection"), ("voice activity", "silero_vad", "speech endpointing"),
              ("speech-to-text", "mlx_whisper", "transcription"), ("Kokoro voice", "kokoro", "the Jarvis voice")]
    for label, mod, what in pieces:
        if _spec(mod):
            out.append(Check(label, OK, f"{mod} installed"))
        else:
            out.append(Check(label, WARN, f"{mod} missing ({what} degraded)", "scripts/install.sh   (or pip install -e '.[voice]')"))
    if _spec("mlx_whisper"):
        if hf_cached(cfg.voice.stt_model):
            out.append(Check("STT model", OK, cfg.voice.stt_model.split("/")[-1]))
        else:
            out.append(Check("STT model", WARN, "not downloaded yet (about 1.5 GB, fetched on first use)", "jarvisd doctor --fix"))
    return out


def check_brew_tools() -> list[Check]:
    out = []
    for tool, what in (("blueutil", "Bluetooth control"), ("brightness", "display brightness")):
        if shutil.which(tool):
            out.append(Check(tool, OK, what))
        else:
            out.append(Check(tool, WARN, f"missing ({what} voice commands won't work)", f"brew install {tool}"))
    return out


def check_disk() -> Check:
    free = shutil.disk_usage(paths.home()).free / 1e9
    if free < MIN_FREE_GB:
        return Check("disk space", WARN, f"{free:.1f} GB free", "models need a few GB; free some space")
    return Check("disk space", OK, f"{free:.0f} GB free")


TERMINAL_NOTE = " (checked from this terminal, which is not the program Jarvis runs as)"
DAEMON_NOTE = " (checked by the running Jarvis itself)"


def daemon_program() -> str:
    """The exact program the daemon runs as: this is what Full Disk Access has to be granted to when Jarvis.app alone isn't enough."""
    return str(Path(sys.executable).resolve())


def check_calendar_contacts(note: str = TERMINAL_NOTE) -> Check:
    from jarvis_core.adapters import apple_data

    try:
        apple_data._open(apple_data.CAL_DB).close()
        return Check("Calendar", OK, "readable" + note)
    except Exception:  # noqa: BLE001
        fix = "Privacy & Security -> Full Disk Access -> add Jarvis"
        if note == DAEMON_NOTE:
            fix = f"Full Disk Access -> + -> press Cmd+Shift+G and add this exact file: {daemon_program()}"
        return Check("Calendar", WARN, "can't read the Calendar database" + note, fix)


ACCESS_FILE = "access.json"
ACCESS_MAX_AGE_S = 900


def write_access_report(cfg: Config) -> None:
    """Called inside the running daemon: what *it* can read, so ``jarvisd doctor`` reports on the real process."""
    checks = [check_calendar_contacts(DAEMON_NOTE), *check_full_disk(cfg, DAEMON_NOTE)]
    payload = {"pid": os.getpid(), "ts": time.time(), "checks": [c.__dict__ for c in checks]}
    path = paths.run_dir() / ACCESS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_access_report() -> list[Check] | None:
    """The daemon's own access results, if a live daemon wrote them recently."""
    try:
        data = json.loads((paths.run_dir() / ACCESS_FILE).read_text(encoding="utf-8"))
        os.kill(int(data["pid"]), 0)  # raises if that daemon is gone
        if time.time() - float(data["ts"]) > ACCESS_MAX_AGE_S:
            return None
        return [Check(**c) for c in data["checks"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check_shell_app() -> list[Check]:
    out: list[Check] = []
    app = Path.home() / "Applications" / "Jarvis.app"
    if app.exists():
        out.append(Check("Jarvis.app", OK, str(app)))
    else:
        out.append(Check("Jarvis.app", WARN, "not built", "scripts/build_shell.sh"))
    status = paths.run_dir() / "shell_status.json"
    try:
        data = json.loads(status.read_text())
    except (OSError, json.JSONDecodeError):
        out.append(Check("shell permissions", WARN, "no report yet (the app hasn't connected)", "open ~/Applications/Jarvis.app"))
        return out
    age = time.time() - float(data.get("ts", 0))
    perms = data.get("permissions") or {}
    missing = [label for key, label in SHELL_PERMS.items() if key in perms and not perms[key]]
    when = "just now" if age < 120 else f"{int(age // 3600)}h ago" if age < 86400 else f"{int(age // 86400)}d ago"
    if missing:
        out.append(Check("shell permissions", WARN, f"missing {', '.join(missing)} (as of {when})",
                         "System Settings → Privacy & Security → enable Jarvis, then restart it. `jarvisd setup` walks through it."))
    elif age > STATUS_STALE_S:
        out.append(Check("shell permissions", WARN, f"report is {int(age // 86400)} days old", "open ~/Applications/Jarvis.app"))
    else:
        out.append(Check("shell permissions", OK, f"mic, accessibility, screen all granted (as of {when})"))
    ipc = paths.ipc_file()
    pid = 0
    try:
        pid = int(json.loads(ipc.read_text()).get("pid") or 0) if ipc.exists() else 0
    except (OSError, ValueError):
        pid = 0
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except PermissionError:
            alive = True
        except OSError:
            alive = False
    out.append(Check("daemon", OK, f"running (pid {pid})") if alive
               else Check("daemon", WARN, "not running" + (" (stale IPC file)" if ipc.exists() else ""),
                          "open ~/Applications/Jarvis.app, or: jarvisd run --standalone"))
    return out


def check_full_disk(cfg: Config, note: str = TERMINAL_NOTE) -> list[Check]:
    """From this terminal by default; the daemon runs it on itself (note=DAEMON_NOTE) so the answer is about the real process."""
    from jarvis_core.policy.presence import read_focus

    out: list[Check] = []
    where = f"Full Disk Access -> + -> press Cmd+Shift+G and add this exact file: {daemon_program()}" if note == DAEMON_NOTE else ""
    on, _ = read_focus()
    out.append(Check("Focus detection", OK, "readable" + note) if on is not None
               else Check("Focus detection", WARN, "can't read Focus state: alerts won't know when you're in Focus" + note,
                          where or "System Settings → Privacy & Security → Full Disk Access → add Jarvis"))
    apps = set(cfg.messaging.apps)
    readers: list[tuple[str, Any]] = []
    if "whatsapp" in apps:
        from jarvis_core.comms.whatsapp_db import WhatsAppDB

        readers.append(("WhatsApp", WhatsAppDB(cfg.messaging.whatsapp_db or None)))
    if "imessage" in apps:
        from jarvis_core.comms.imessage import IMessageDB

        readers.append(("iMessage", IMessageDB(cfg.messaging.imessage_db or None)))
    for label, reader in readers:
        try:
            ok, reason = reader.check()
        except Exception as e:
            ok, reason = False, str(e)
        if ok:
            out.append(Check(label, OK, "database readable and schema recognised" + note))
        elif "Full Disk Access" in reason:
            out.append(Check(label, WARN, reason + note, where or "Privacy & Security → Full Disk Access → add Jarvis (and your terminal for this check)"))
        else:
            out.append(Check(label, WARN, reason or "unavailable", "Open the app once so its database exists; if the schema changed, Jarvis disables it safely"))
    return out


def check_gmail(cfg: Config) -> Check | None:
    if "gmail" not in cfg.messaging.apps:
        return None
    try:
        from jarvis_core.adapters.services import GoogleSession

        s = GoogleSession("gmail")
        if not s.connected():
            return Check("Gmail", WARN, "not signed in", "jarvisd setup   (Google sign-in step)")
        if not s.valid():
            return Check("Gmail", WARN, "Google no longer honours the saved sign-in (revoked or expired)", "jarvisd google --reauth")
        scopes = s.scopes()
    except Exception as e:
        return Check("Gmail", WARN, f"sign-in support unavailable ({e.__class__.__name__})", "run `jarvisd google`")
    if "gmail.send" in scopes:
        return Check("Gmail", WARN, "token carries the send scope. Jarvis never uses it, but you'd be safer without it",
                     "re-authorise with only gmail.modify, and revoke the old grant at myaccount.google.com/permissions")
    if not any(sc in scopes for sc in ("gmail.modify", "gmail.compose")):
        return Check("Gmail", WARN, "token can read but not create drafts", "re-run the Google sign-in with draft access")
    return Check("Gmail", OK, "signed in; can read and create drafts (cannot send)")


def check_google_workspace(cfg: Config) -> Check | None:
    if "gmail" not in cfg.messaging.apps:
        return None
    try:
        from jarvis_core.adapters.google import GoogleSession

        s = GoogleSession("gmail")
        if not s.connected():
            return None
        scopes = s.scopes()
    except Exception:
        return None
    cal, drive = "auth/calendar" in scopes, "auth/drive.readonly" in scopes or "auth/drive " in scopes + " "
    if cal and drive:
        return Check("Google Calendar & Drive", OK, "Calendar (read and add events) and Drive (read only) connected")
    missing = " and ".join(n for n, ok in (("Calendar", cal), ("Drive", drive)) if not ok)
    return Check("Google Calendar & Drive", WARN, f"{missing} not granted yet", "jarvisd google --reauth   (approve Calendar and Drive in the browser)")


def check_data_files() -> list[Check]:
    out = []
    for label, path in (("memory.db", paths.memory_db()), ("state.db", paths.state_db())):
        if not path.exists():
            out.append(Check(label, OK, "not created yet"))
            continue
        import sqlite3

        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            res = con.execute("PRAGMA quick_check").fetchone()[0]
            con.close()
            out.append(Check(label, OK if res == "ok" else FAIL, "healthy" if res == "ok" else f"corrupt ({res})",
                             "" if res == "ok" else f"move {path} aside; Jarvis recreates it"))
        except sqlite3.Error as e:
            out.append(Check(label, FAIL, f"unreadable ({e})", f"move {path} aside; Jarvis recreates it"))
    return out


# -------------------------------------------------------------------------------------- network checks


async def check_nim(cfg: Config, llm: Any | None = None, *, probe_tools: bool = True) -> list[Check]:
    if not secrets.get_secret("NIM_API_KEY") and llm is None:
        return []
    if llm is None:
        from jarvis_core.brain.llm import build_llm

        llm = build_llm(cfg)
    loop = asyncio.get_running_loop()
    try:
        healthy = await loop.run_in_executor(None, llm.health)
    except Exception:
        healthy = False
    if not healthy:
        return [Check("NIM reachable", FAIL, "no answer from NVIDIA NIM (offline, or the key was rejected)",
                      "check the network and `jarvisd set-key`; Jarvis falls back to template briefs without it")]
    out = [Check("NIM reachable", OK, cfg.llm.host)]
    out.extend(await check_models(cfg, llm))
    if probe_tools:
        out.append(await _probe_tool_calling(llm, cfg))
    return out


async def check_models(cfg: Config, llm: Any) -> list[Check]:
    """Does each configured model actually answer? (NIM's catalogue lists models that are gone, so ask them.)"""
    probe = getattr(llm, "probe", None)
    out: list[Check] = []
    if probe is not None:
        seen: dict[str, tuple[bool, str]] = {}
        roles = (("chat", cfg.llm.chat_model), ("fast", cfg.llm.fast_model), ("vision", cfg.llm.vision_model))
        for _, name in roles:
            if name not in seen:
                seen[name] = await probe(name)
        for role, name in roles:
            ok, why = seen[name]
            if ok:
                out.append(Check(f"{role} model", OK, name))
            elif cfg.llm.fallback_models:
                out.append(Check(f"{role} model", WARN, f"{name} isn't answering ({why}); falling back to {cfg.llm.fallback_models[0]}",
                                 "jarvisd config edit   ([llm] section)"))
            else:
                out.append(Check(f"{role} model", FAIL, f"{name} isn't answering ({why})", "jarvisd config edit   ([llm] section)"))
        return out
    try:
        models = set(await asyncio.get_running_loop().run_in_executor(None, llm.list_models))
    except Exception:
        models = set()
    if models:
        for role, name in (("chat", cfg.llm.chat_model), ("fast", cfg.llm.fast_model), ("vision", cfg.llm.vision_model)):
            if name not in models:
                out.append(Check(f"{role} model", WARN, f"{name} isn't in your NIM catalogue", "jarvisd config edit   ([llm] section)"))
    return out


PING_TOOL = {"type": "function", "function": {
    "name": "ping", "description": "Reply to the user by calling this tool.",
    "parameters": {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}}}


async def _probe_tool_calling(llm: Any, cfg: Config) -> Check:
    from jarvis_core.interfaces import ToolCallEvent

    msgs = [{"role": "system", "content": "Always answer by calling the ping tool."},
            {"role": "user", "content": "Call ping with the word hello."}]

    async def go() -> bool:
        async for ev in llm.stream_chat(msgs, [PING_TOOL], role="chat", temperature=0.0, max_tokens=300):
            if isinstance(ev, ToolCallEvent):
                return True
        return False

    try:
        ok = await asyncio.wait_for(go(), 30)
    except Exception as e:
        return Check("tool calling", WARN, f"couldn't test ({e.__class__.__name__})")
    if ok:
        return Check("tool calling", OK, f"{cfg.llm.chat_model} calls tools")
    return Check("tool calling", FAIL, f"{cfg.llm.chat_model} did not call the tool", "choose a model with function calling in [llm] chat_model")


# ----------------------------------------------------------------------------------------- fixes


def apply_fixes(cfg: Config, say: Callable[[str], None] = print) -> None:
    """Download what's missing. Never touches permissions or secrets."""
    if _spec("openwakeword"):
        try:
            import openwakeword

            say("downloading wake word models…")
            openwakeword.utils.download_models()
        except Exception as e:
            say(f"  wake word download failed: {e}")
    if _spec("huggingface_hub"):
        from huggingface_hub import snapshot_download

        repos = []
        if _spec("mlx_whisper") and not hf_cached(cfg.voice.stt_model):
            repos.append(cfg.voice.stt_model)
        for repo in repos:
            say(f"downloading {repo}…")
            try:
                snapshot_download(repo)
            except Exception as e:
                say(f"  failed: {e}")
    if _spec("kokoro"):
        try:
            from jarvis_core.voice.tts import KokoroTTS

            say("preparing the Kokoro voice (first run also fetches its language model)…")
            KokoroTTS(cfg.voice.tts_voice, cfg.voice.tts_speed).warm_up()
        except Exception as e:
            say(f"  Kokoro preparation failed: {e}")
    from jarvis_core.housekeeping import TranscriptLog

    n = TranscriptLog(cfg.safety.transcript_retention_days).prune()
    if n:
        say(f"removed {n} old transcript file(s)")


# ----------------------------------------------------------------------------------------- driver


def check_local_model(cfg: Config) -> Check | None:
    """The offline fallback: Ollama running here with the configured model pulled."""
    if not cfg.llm.local_fallback:
        return None
    import httpx

    try:
        names = [m.get("name", "") for m in httpx.get(cfg.llm.local_host.rstrip("/") + "/api/tags", timeout=2).json().get("models", [])]
    except Exception:  # noqa: BLE001
        return Check("offline model", WARN, f"Ollama isn't running at {cfg.llm.local_host}, so Jarvis has no answers when offline",
                     "start Ollama (ollama.com), then: ollama pull " + cfg.llm.local_model)
    if any(n == cfg.llm.local_model or n.split(":")[0] == cfg.llm.local_model for n in names):
        return Check("offline model", OK, f"{cfg.llm.local_model} ready in Ollama")
    return Check("offline model", WARN, f"{cfg.llm.local_model} isn't pulled in Ollama", f"ollama pull {cfg.llm.local_model}")


async def collect_checks(*, llm: Any | None = None, network: bool = True) -> list[Check]:
    checks: list[Check] = []
    cfg_check, cfg = check_config()
    checks.append(cfg_check)
    if cfg is None:
        return checks
    checks.append(check_nim_key())
    if network:
        checks.extend(await check_nim(cfg, llm))
    local = check_local_model(cfg)
    if local:
        checks.append(local)
    checks.extend(check_voice_stack(cfg))
    checks.extend(check_brew_tools())
    access = load_access_report()  # prefer what the running daemon itself can read over what this terminal can read
    checks.extend(access[:1] if access else [check_calendar_contacts()])
    checks.extend(check_shell_app())
    checks.extend(access[1:] if access else check_full_disk(cfg))
    gm = check_gmail(cfg)
    if gm:
        checks.append(gm)
    gw = check_google_workspace(cfg)
    if gw:
        checks.append(gw)
    checks.extend(check_data_files())
    checks.append(check_disk())
    return checks


def summarise(checks: list[Check]) -> tuple[int, int, int]:
    return tuple(sum(1 for c in checks if c.status == s) for s in (OK, WARN, FAIL))  # type: ignore[return-value]


async def run_doctor(fix: bool = False, *, llm: Any | None = None, network: bool = True,
                     out: Callable[[str], Awaitable[None] | None] | None = None) -> int:
    write = out or print
    if fix:
        _, cfg = check_config()
        if cfg:
            await asyncio.get_running_loop().run_in_executor(None, apply_fixes, cfg, write)
    checks = await collect_checks(llm=llm, network=network)
    write("Jarvis doctor")
    for c in checks:
        write(c.line())
    ok, warn, fail = summarise(checks)
    write(f"\n{ok} ok, {warn} to look at, {fail} broken")
    return 1 if fail else 0


def main() -> int:  # pragma: no cover
    return asyncio.run(run_doctor())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
