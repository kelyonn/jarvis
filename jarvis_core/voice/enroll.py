"""Teaching Jarvis your voice, as a conversation: it asks for a phrase, waits for you, then asks for the next one.

It runs through the normal turn loop (no nested listening): each phrase you say is one turn, the daemon routes it here
instead of to the assistant, and the follow-up window stays open between phrases.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from jarvis_core import paths

if TYPE_CHECKING:  # pragma: no cover
    from jarvis_core.daemon import Daemon

log = logging.getLogger(__name__)

# Varied sounds, spoken the way you would normally talk to Jarvis (same mic, same room, same distance).
PHRASES = [
    "Hey Jarvis, what's the weather like today?",
    "Open Spotify and play my favourite playlist.",
    "Remind me to call my mother tomorrow at nine.",
    "What's on my calendar this afternoon?",
    "Set a timer for twenty minutes, please.",
    "Jarvis, read me my unread messages.",
]
MIN_SECONDS = 1.2
MIN_MATCH = 45  # how closely (percent) what was heard must resemble the phrase to count as that phrase
WINDOW_S = 25.0  # how long Jarvis waits for each phrase
EXPIRES_S = 180.0  # an abandoned session ends by itself
CANCEL = {"stop", "cancel", "never mind", "nevermind", "forget it", "that's enough", "stop it"}


class EnrollSession:
    def __init__(self) -> None:
        self.index = 0
        self.samples: list[bytes] = []
        self.misses = 0
        self.touched = time.monotonic()

    @property
    def phrase(self) -> str:
        return PHRASES[self.index]

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.touched > EXPIRES_S


def begin(daemon: Daemon) -> str:
    """Start a session; returns what Jarvis says first."""
    if daemon.verifier is None:
        return "Voice recognition isn't installed, sir. It needs the resemblyzer package."
    daemon._enroll = EnrollSession()
    return (f"Very good, sir. I'll learn your voice. I'll give you {len(PHRASES)} phrases, one at a time; say each in your "
            f"normal voice and I'll wait for you. Say cancel at any time. First: {PHRASES[0]}")


async def step(daemon: Daemon, audio: bytes, text: str) -> str:
    """One phrase from the user. Returns what Jarvis says next; clears ``daemon._enroll`` when the session ends."""
    from rapidfuzz import fuzz

    s: EnrollSession = daemon._enroll  # type: ignore[assignment]
    s.touched = time.monotonic()
    said = " ".join((text or "").lower().replace("'", "").split()).strip(" .!?")
    if said in CANCEL or (said.startswith("cancel") and len(said.split()) <= 3):
        daemon._enroll = None
        return "No problem, sir. I'll leave your voice as it was."
    seconds = len(audio) / 32000
    heard_ok = bool(text) and fuzz.partial_ratio(said, " ".join(s.phrase.lower().replace("'", "").split())) >= MIN_MATCH
    if seconds < MIN_SECONDS or not heard_ok:
        s.misses += 1
        if s.misses >= 4:
            daemon._enroll = None
            return "I'm having trouble hearing you clearly, sir. Let's try again when it's quieter."
        return f"I didn't quite catch that. Please say: {s.phrase}"
    s.misses = 0
    s.samples.append(audio)
    s.index += 1
    if s.index < len(PHRASES):
        return f"Good. Next: {s.phrase}"
    verifier = daemon.verifier
    samples, daemon._enroll = s.samples, None
    try:
        used = await asyncio.to_thread(verifier.enroll, samples)  # type: ignore[union-attr]
    except ValueError:
        return "I couldn't use those recordings, sir. Say learn my voice to try again."
    log.info("voiceprint saved from %d phrases", used)
    return ("That's it, sir. I've learned your voice. From now on I'll only act on you. "
            "If I ever miss you, press Option Space, or say forget my voice.")


async def watch_requests(daemon: Daemon, interval: float = 1.5) -> None:
    """``jarvisd voice-id enroll`` drops ~/.jarvis/run/voice_id.json; this picks it up."""
    req_path = paths.run_dir() / "voice_id.json"
    while True:
        await asyncio.sleep(interval)
        if not req_path.exists():
            continue
        try:
            action = json.loads(req_path.read_text() or "{}").get("action")
        except (OSError, ValueError):
            action = None
        req_path.unlink(missing_ok=True)
        if action == "enroll" and daemon.is_free:
            await daemon.start_enrollment()
        elif action == "forget" and daemon.verifier is not None:
            daemon.verifier.forget()
            log.info("voiceprint removed")
