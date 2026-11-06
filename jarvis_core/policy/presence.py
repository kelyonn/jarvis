"""Is the user busy? Focus mode, a call (mic/camera used by another app), screen sharing, or a manual snooze."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

ASSERTIONS = Path.home() / "Library/DoNotDisturb/DB/Assertions.json"
MODE_CONFIG = Path.home() / "Library/DoNotDisturb/DB/ModeConfigurations.json"


def read_focus(assertions: Path = ASSERTIONS, modes: Path = MODE_CONFIG) -> tuple[bool | None, str]:
    """(is_focus_on, mode_name). ``None`` means it couldn't be determined (e.g. no Full Disk Access)."""
    try:
        data = json.loads(assertions.read_text())
    except FileNotFoundError:
        return legacy_dnd(), ""
    except PermissionError:
        return None, ""
    except (OSError, json.JSONDecodeError):
        return None, ""
    records = []
    for entry in data.get("data", []):
        records.extend(entry.get("storeAssertionRecords", []) or [])
    if not records:
        return False, ""
    name = ""
    mode_id = (records[0].get("assertionDetails") or {}).get("assertionDetailsModeIdentifier", "")
    try:
        cfg = json.loads(modes.read_text())
        for entry in cfg.get("data", []):
            for mc in (entry.get("modeConfigurations") or {}).values():
                m = mc.get("mode") or {}
                if m.get("modeIdentifier") == mode_id:
                    name = m.get("name", "")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return True, name


def legacy_dnd() -> bool | None:
    try:
        out = subprocess.run(
            ["defaults", "-currentHost", "read", "com.apple.notificationcenterui", "doNotDisturb"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            return out.stdout.strip() == "1"
    except Exception:
        pass
    return None


# Helpers that only run while a screen is being shared. Best effort: apps that share without a helper are not seen.
SHARING_PROCESSES = {"screensharingd", "ScreensharingAgent", "CptHost", "zoom.us Share", "AirPlayUIAgent-sharing"}


def detect_screen_sharing() -> bool:
    """True when a macOS Screen Sharing session or a Zoom screen share helper is running."""
    try:
        import psutil

        return any((p.info.get("name") or "") in SHARING_PROCESSES for p in psutil.process_iter(["name"]))
    except Exception:  # noqa: BLE001
        return False


class Presence:
    """Combines Focus, call detection and snooze into one ``can_speak`` decision."""

    def __init__(
        self,
        *,
        focus_reader: Callable[[], tuple[bool | None, str]] = read_focus,
        clock: Callable[[], float] = time.time,
        focus_cache_s: float = 15.0,
        sharing_reader: Callable[[], bool] = detect_screen_sharing,
    ) -> None:
        self._read_focus = focus_reader
        self._clock = clock
        self._focus_cache_s = focus_cache_s
        self._read_sharing = sharing_reader
        self._sharing_at = -1e9
        self._sharing = False
        self._focus_at = -1e9
        self._focus: tuple[bool | None, str] = (False, "")
        self.mic_in_use = False
        self.camera_in_use = False
        self.call_app = ""
        self.snooze_until = 0.0

    # -- inputs ---------------------------------------------------------------
    def on_device_usage(self, mic: bool, camera: bool, app_hint: str) -> None:
        self.mic_in_use, self.camera_in_use, self.call_app = mic, camera, app_hint

    def snooze(self, minutes: float) -> None:
        self.snooze_until = self._clock() + minutes * 60

    def unsnooze(self) -> None:
        self.snooze_until = 0.0

    # -- state ------------------------------------------------------------------
    def focus(self) -> tuple[bool | None, str]:
        now = self._clock()
        if now - self._focus_at >= self._focus_cache_s:
            try:
                self._focus = self._read_focus()
            except Exception:
                log.debug("focus read failed", exc_info=True)
                self._focus = (None, "")
            self._focus_at = now
        return self._focus

    def screen_sharing(self) -> bool:
        now = self._clock()
        if now - self._sharing_at >= 10.0:
            try:
                self._sharing = bool(self._read_sharing())
            except Exception:  # noqa: BLE001
                self._sharing = False
            self._sharing_at = now
        return self._sharing

    @property
    def on_call(self) -> bool:
        return self.mic_in_use or self.camera_in_use

    def state(self) -> str:
        """'free' | 'call' | 'sharing' | 'focus' | 'snoozed'."""
        if self._clock() < self.snooze_until:
            return "snoozed"
        if self.on_call:
            return "call"
        if self.screen_sharing():
            return "sharing"
        on, _ = self.focus()
        if on:
            return "focus"
        return "free"

    def can_speak(self) -> bool:
        return self.state() == "free"

    def focus_label(self) -> str:
        on, name = self.focus()
        return (name or "on") if on else "off"
