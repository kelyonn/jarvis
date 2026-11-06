"""``jarvisd setup``: the first-run wizard (terminal, with Jarvis speaking the key moments)."""

from __future__ import annotations

import asyncio
import getpass
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from jarvis_core import paths, secrets
from jarvis_core.config import Config, RoutineCfg, _hhmm, load_config, save_config

PANES = [
    ("Microphone", "Privacy_Microphone", "so I can hear you"),
    ("Accessibility", "Privacy_Accessibility", "to control windows and apps, and read what's on screen"),
    ("Screen Recording", "Privacy_ScreenCapture", "to look at your screen when you ask me to"),
    ("Full Disk Access", "Privacy_AllFiles", "to read WhatsApp and iMessage, and to know when Focus is on"),
    ("Automation", "Privacy_Automation", "to drive Spotify, Notes, Mail and Calendar (macOS also asks the first time)"),
    ("Calendars", "Privacy_Calendars", "so I can read your meetings"),
    ("Reminders", "Privacy_Reminders", "so I can read and add reminders"),
]
WAKE_TARGET = 3
WAKE_MIN, WAKE_MAX = 0.35, 0.7


# --------------------------------------------------------------------------------- pure helpers


def threshold_from_peaks(peaks: list[float], default: float = 0.5) -> float:
    """A bit under the weakest of your 'Hey Jarvis' peaks, kept within sane bounds."""
    if not peaks:
        return default
    return round(min(WAKE_MAX, max(WAKE_MIN, min(peaks) * 0.8)), 2)


class PeakTracker:
    """Turns a stream of wake scores into one peak per utterance."""

    def __init__(self, high: float = 0.2, low: float = 0.08) -> None:
        self.high, self.low = high, low
        self.peaks: list[float] = []
        self._cur = 0.0

    def feed(self, score: float) -> bool:
        """True when an utterance just finished and a peak was recorded."""
        if score >= self.high:
            self._cur = max(self._cur, score)
            return False
        if self._cur and score <= self.low:
            self.peaks.append(self._cur)
            self._cur = 0.0
            return True
        return False


_TIME = re.compile(r"(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?", re.I)


def parse_times(text: str, default: list[str]) -> list[str]:
    """'1pm, 7:30 pm 21' -> ['13:00', '19:30', '21:00']. Empty input keeps ``default``."""
    text = (text or "").strip()
    if not text:
        return default
    leftover = _TIME.sub("", text).strip(" ,;")
    if leftover:
        raise ValueError(f"'{leftover}' isn't a time")
    out = []
    for m in _TIME.finditer(text):
        h, mi, ap = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        out.append(_hhmm(f"{h:02d}:{mi:02d}"))
    return out


def detect_timezone(default: str) -> str:
    try:
        link = os.readlink("/etc/localtime")
        name = link.split("zoneinfo/")[-1]
        ZoneInfo(name)
        return name
    except (OSError, ValueError, KeyError, Exception):
        return default


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------- IO


@dataclass
class IO:
    ask: Callable[[str], str] = input
    secret: Callable[[str], str] = getpass.getpass
    out: Callable[[str], None] = print
    speak: Callable[[str], None] | None = None
    open_pane: Callable[[str], None] = field(default=lambda anchor: subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?{anchor}"], check=False))
    open_app: Callable[[Path], None] = field(default=lambda p: subprocess.run(["open", str(p)], check=False))


def macos_say(text: str) -> None:
    try:
        subprocess.Popen(["say", "-v", "Daniel", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class Wizard:
    def __init__(self, io: IO, llm=None) -> None:
        self.io, self.llm = io, llm
        self.notes: list[str] = []

    # -- helpers
    def say(self, text: str, *, speak: bool = False) -> None:
        self.io.out(text)
        if speak and self.io.speak:
            self.io.speak(text)

    def ask(self, prompt: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        ans = self.io.ask(f"{prompt}{suffix}: ").strip()
        return ans or default

    def yes(self, prompt: str, default: bool = True) -> bool:
        ans = self.io.ask(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not ans else ans.startswith("y")

    def step(self, n: int, title: str) -> None:
        self.io.out(f"\n{n}. {title}")

    def cfg(self) -> Config:
        return load_config()

    # -- steps
    def identity(self) -> None:
        self.step(1, "Who are you?")
        cfg = self.cfg()
        name = self.ask("What should I call you", cfg.user.name)
        tz = self.ask("Timezone", detect_timezone(cfg.user.timezone))
        while not valid_timezone(tz):
            self.io.out(f"  '{tz}' isn't a timezone (try something like Asia/Kolkata).")
            tz = self.ask("Timezone", detect_timezone(cfg.user.timezone))
        city = self.ask("City for the weather (Enter for automatic)", cfg.devices.location_city)
        cfg.user.name, cfg.user.timezone, cfg.devices.location_city = name, tz, city
        save_config(cfg)
        self.say(f"Very good, {name}.", speak=True)

    async def nim_key(self) -> None:
        self.step(2, "NVIDIA NIM key (my brain)")
        from jarvis_core.brain.llm import build_llm

        key = secrets.get_secret("NIM_API_KEY")
        if key:
            self.io.out("  A key is already stored in your Keychain.")
        else:
            self.io.out("  Get a free key at build.nvidia.com, then paste it here (input is hidden).")
            key = self.io.secret("  NIM API key (nvapi-…): ").strip()
            if not key:
                self.io.out("  Skipped. Run `jarvisd set-key` any time.")
                return
            secrets.set_secret("NIM_API_KEY", key)
            self.io.out("  Stored in Keychain.")
        llm = self.llm or build_llm(self.cfg())
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, llm.health):
            self.io.out("  ✗ NIM didn't accept that key (or you're offline). Re-run `jarvisd set-key`.")
            return
        from jarvis_core.doctor import check_models

        bad = [c for c in await check_models(self.cfg(), llm) if c.status != "ok"]
        self.io.out("  ✓ NIM answered." + ("".join(f"\n  ! {c.name}: {c.detail}" for c in bad)))

    def permissions(self) -> None:
        self.step(3, "Permissions")
        app = Path.home() / "Applications" / "Jarvis.app"
        if app.exists():
            self.io.out("  Launching Jarvis.app so it appears in the lists…")
            self.io.open_app(app)
        else:
            self.io.out("  Jarvis.app isn't built yet (scripts/build_shell.sh); you can still grant these now.")
        for label, anchor, why in PANES:
            self.io.out(f"  • {label}: {why}.")
            if self.yes("    Open that pane?"):
                self.io.open_pane(anchor)
                self.io.ask("    Switch Jarvis on, then press Enter… ")
        self.io.out("  Quit and reopen Jarvis from the menu bar so the new permissions take effect.")

    def wake_calibration(self) -> None:
        self.step(4, "Wake word calibration")
        if not self.yes("  Say “Hey Jarvis” three times to tune sensitivity?"):
            return
        try:
            peaks = calibrate_wake(self.cfg().activation.wake_word, self.io.out)
        except Exception as e:
            self.io.out(f"  Skipped ({e}). The default sensitivity is fine; tune activation.wake_threshold later.")
            return
        if len(peaks) < WAKE_TARGET:
            self.io.out("  I only heard you " + str(len(peaks)) + " time(s); keeping the current sensitivity.")
            return
        cfg = self.cfg()
        cfg.activation.wake_threshold = threshold_from_peaks(peaks)
        save_config(cfg)
        self.io.out(f"  ✓ Sensitivity set to {cfg.activation.wake_threshold}.")

    def google(self) -> None:
        self.step(5, "Google (Gmail drafts)")
        cfg = self.cfg()
        if "gmail" not in cfg.messaging.apps:
            self.io.out("  Gmail is switched off in [messaging] apps; skipping.")
            return
        try:
            from jarvis_core.adapters import services as oj
        except ImportError:
            self.io.out("  Skipped: Google sign-in support is unavailable.")
            return
        try:
            session = oj.GoogleSession("gmail")
            if session.connected() and not session.valid():
                self.io.out("  The saved Google sign-in was revoked or has expired; let's sign in again.")
                oj.reset_google("gmail")
            if session.connected():
                extra = oj.google_extra_scopes("gmail")
                if extra:
                    names = ", ".join(sorted({x.rsplit("/", 1)[-1] for x in extra}))
                    self.io.out(f"  Signed in, but with broader access than I need ({names}).")
                    if self.yes("  Redo the sign-in with Gmail access only (revokes the old grant)?"):
                        oj.reset_google("gmail")
                        oj.run_google_oauth("gmail")
                        self.io.out("  ✓ Done." if oj.GoogleSession("gmail").connected() else "  ✗ Sign-in didn't complete; run `jarvisd google --reauth`.")
                    return
                self.io.out("  ✓ Already signed in. I can read mail and create drafts, never send.")
                return
            if not self.yes("  Sign in to Google in your browser now?"):
                return
            if not oj.has_google_client("gmail"):
                self.io.out(
                    "  Google needs a one-time OAuth client for your own account (free, about 5 minutes):\n"
                    f"    1. Open {oj.GOOGLE_CONSOLE_URL}, create or pick a project.\n"
                    "    2. APIs & Services → Library → enable the Gmail API.\n"
                    "    3. OAuth consent screen: type External, add yourself as a test user.\n"
                    "    4. Credentials → Create credentials → OAuth client ID → application type Desktop app.\n"
                    "    5. Copy the client ID and client secret it shows."
                )
                if not self.yes("  Do you have the client ID and secret to paste now?", default=False):
                    self.io.out("  Skipped. Run `jarvisd setup` again when you have them; Gmail stays off until then.")
                    return
                client_id = self.ask("  Client ID")
                client_secret = self.io.secret("  Client secret (hidden): ").strip()
                if not client_id or not client_secret:
                    self.io.out("  Skipped: I need both values.")
                    return
                oj.save_google_client(client_id, client_secret, "gmail")
            oj.run_google_oauth("gmail")
            self.io.out("  ✓ Done." if oj.GoogleSession("gmail").connected() else "  ✗ Sign-in didn't complete; run `jarvisd setup` again.")
        except Exception as e:
            self.io.out(f"  Google sign-in failed ({e.__class__.__name__}: {str(e)[:120]}). Run `jarvisd setup` to retry.")

    def messaging_access(self) -> None:
        self.step(6, "Can I read WhatsApp and iMessage?")
        from jarvis_core.doctor import check_full_disk

        for c in check_full_disk(self.cfg()):
            self.io.out(c.line())

    def vips(self) -> None:
        self.step(7, "VIPs: people I should interrupt you for")
        cfg = self.cfg()
        try:
            from jarvis_core.comms.whatsapp_db import WhatsAppDB

            chats = [c.name for c in WhatsAppDB(cfg.messaging.whatsapp_db or None).chats(60) if not c.is_group and c.name][:8]
        except Exception:
            chats = []
        if chats:
            self.io.out("  Your most recent WhatsApp chats:")
            for i, name in enumerate(chats, 1):
                self.io.out(f"    {i}. {name}")
        ans = self.ask("  Pick VIPs by number or type names, comma separated (Enter to skip)" if chats
                       else "  VIP names, comma separated (Enter to skip)")
        if ans:
            from jarvis_core.comms.vip import VipList

            vip = VipList(cfg)
            for n in pick_names(ans, chats):
                vip.add(n)
            self.io.out("  ✓ VIPs: " + ", ".join(vip.names()))

    def schedule(self) -> None:
        self.step(8, "Briefings, quiet hours and routines")
        cfg = self.cfg()
        while True:
            try:
                cfg.briefing.slots = parse_times(self.ask("  Extra brief times (24h or 1pm, comma separated)", ", ".join(cfg.briefing.slots)),
                                                 cfg.briefing.slots)
                break
            except ValueError as e:
                self.io.out(f"  {e}")
        while True:
            try:
                qh = self.ask("  Quiet hours, no spoken alerts (start-end)", "-".join(cfg.user.quiet_hours))
                start, end = parse_times(re.sub(r"\s*(?:-|to)\s*", " ", qh), cfg.user.quiet_hours)
                cfg.user.quiet_hours = [start, end]
                break
            except (ValueError, TypeError) as e:
                self.io.out(f"  Give two times like 23:30-07:00 ({e}).")
        for mode in ("work", "study"):
            if mode in cfg.routines:
                continue
            apps = self.ask(f"  Apps to open for “{mode} mode” (comma separated, Enter to skip)")
            if apps:
                focus = self.ask(f"  Shortcuts name that turns on a Focus for {mode} mode (Enter for none)") or None
                cfg.routines[mode] = RoutineCfg(apps=[a.strip() for a in apps.split(",") if a.strip()], focus=focus)
        save_config(cfg)
        self.io.out("  ✓ Saved. Window layouts can be added under [routines.<name>.layout] in config.toml.")

    async def test_brief(self) -> None:
        self.step(9, "Test brief")
        if not self.yes("  Run a briefing now (printed here)?"):
            return
        from jarvis_core.briefing.cli import run_brief

        await run_brief(mode="full", speak=False)


def calibrate_wake(wake_word: str, out: Callable[[str], None], seconds: float = 45.0) -> list[float]:
    """Listen on the default mic and return one score peak for each “Hey Jarvis” heard."""
    import time

    import numpy as np
    import sounddevice as sd

    from jarvis_core.voice.wake import CHUNK_SAMPLES, OpenWakeWordDetector

    det = OpenWakeWordDetector(wake_word, threshold=2.0)  # never fires; we only read scores
    tracker = PeakTracker()
    out("  Listening… say “Hey Jarvis” normally, pausing between each.")
    deadline = time.monotonic() + seconds
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=CHUNK_SAMPLES) as stream:
        while time.monotonic() < deadline and len(tracker.peaks) < WAKE_TARGET:
            data, _ = stream.read(CHUNK_SAMPLES)
            det.process(np.asarray(data).astype(np.int16).tobytes())
            if tracker.feed(det.last_score):
                out(f"  heard you ({len(tracker.peaks)}/{WAKE_TARGET}, strength {tracker.peaks[-1]:.2f})")
    return tracker.peaks


def pick_names(answer: str, chats: list[str]) -> list[str]:
    """'1, 3, Mom' -> the first and third listed chat plus 'Mom'. Numbers outside the list are ignored."""
    out: list[str] = []
    for part in (x.strip() for x in answer.split(",")):
        if not part:
            continue
        if part.isdigit():
            i = int(part)
            if 1 <= i <= len(chats):
                out.append(chats[i - 1])
        else:
            out.append(part)
    return list(dict.fromkeys(out))


async def run_setup(non_interactive: bool = False, *, io: IO | None = None, llm=None) -> int:
    cfg_path = paths.config_file()
    if non_interactive:
        cfg = load_config()
        if not cfg_path.exists():
            save_config(cfg)
        env_key = os.environ.get("NIM_API_KEY")
        if env_key and not secrets.get_secret("NIM_API_KEY", env_fallback=False):
            secrets.set_secret("NIM_API_KEY", env_key)
        print(f"config at {cfg_path}")
        from jarvis_core.doctor import run_doctor

        return await run_doctor(network=False)

    if not cfg_path.exists():
        save_config(load_config())
    w = Wizard(io or IO(speak=macos_say), llm)
    w.say("Jarvis setup. Press Ctrl-C at any time; everything is saved as you go.", speak=True)
    w.identity()
    await w.nim_key()
    w.permissions()
    w.wake_calibration()
    w.google()
    w.messaging_access()
    w.vips()
    w.schedule()
    await w.test_brief()
    w.io.out("\nAll set. Run `jarvisd doctor` for a health check; Jarvis will start at login.")
    w.say("Setup complete. At your service.", speak=True)
    return 0
