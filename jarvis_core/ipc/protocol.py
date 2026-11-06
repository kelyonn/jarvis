"""Wire protocol between jarvis_core and JarvisShell (mirrored in Protocol.swift).

Transport: WebSocket on 127.0.0.1. Two kinds of frames:

* binary: 1-byte type header.
    ``0x01`` mic PCM        (Shell -> core)  16 kHz mono int16, 20 ms (640 bytes)
    ``0x02`` TTS audio      (core -> Shell)  16-byte utterance id + 24 kHz mono int16
* text: JSON ``{"type": ..., "id": ..., "data": {...}}``
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

MIC_FRAME = 0x01
TTS_FRAME = 0x02
TTS_SAMPLE_RATE = 24000
MIC_SAMPLE_RATE = 16000
PING_INTERVAL_S = 5.0
MISSED_PINGS_LIMIT = 3

# Shell -> core
HELLO = "hello"
HOTKEY = "hotkey"
LIFECYCLE = "lifecycle"
DEVICE_USAGE = "device_usage"
PLAYBACK = "playback"
AX_RESULT = "ax_result"
LOCATION = "location"
ORB_CLICK = "orb_click"
TEXT_INPUT = "text_input"  # something you typed into the orb panel instead of saying
COMMAND = "command"
PING = "ping"
PONG = "pong"

# core -> Shell
WELCOME = "welcome"
STATE = "state"
CAPTION = "caption"
PLAY_BEGIN = "play_begin"
PLAY_END = "play_end"
STOP_AUDIO = "stop_audio"
EARCON = "earcon"
ORB_QUEUE = "orb_queue"
HIDE_UI = "hide_ui"
RICH = "rich"
AX_REQUEST = "ax_request"
INPUT_REQUEST = "input_request"
SCREENSHOT_REQUEST = "screenshot_request"
LOCATION_REQUEST = "location_request"
PERMISSION_PROMPT = "permission_prompt"
NOTIFY = "notify"
MIC = "mic"
MIC_OPEN = "mic_open"  # open/close the capture device itself (the OS mic indicator follows this)

REQUEST_TYPES = {
    "ax": AX_REQUEST,
    "input": INPUT_REQUEST,
    "screenshot": SCREENSHOT_REQUEST,
    "location": LOCATION_REQUEST,
}

HOTKEY_ACTIONS = {"down", "up", "long_press"}
LIFECYCLE_EVENTS = {
    "will_sleep", "did_wake", "screen_locked", "screen_unlocked", "screens_slept", "screens_woke",
}


@dataclass
class Control:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def encode(self) -> str:
        return json.dumps({"type": self.type, "id": self.id or uuid.uuid4().hex[:8], "data": self.data},
                          separators=(",", ":"))

    @staticmethod
    def decode(text: str) -> Control:
        obj = json.loads(text)
        if not isinstance(obj, dict) or "type" not in obj:
            raise ValueError("malformed control frame")
        data = obj.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("control data must be an object")
        return Control(str(obj["type"]), data, str(obj.get("id", "")))


def split_op(op: str) -> tuple[str, str]:
    """``"ax.move"`` -> (``"ax_request"``, ``"move"``); ``"screenshot"`` -> (..., ``""``)."""
    head, _, rest = op.partition(".")
    if head not in REQUEST_TYPES:
        raise ValueError(f"unknown request op {op!r}")
    return REQUEST_TYPES[head], rest


def encode_mic_frame(pcm16: bytes) -> bytes:
    return bytes([MIC_FRAME]) + pcm16


def encode_tts_frame(utterance_hex: str, pcm16: bytes) -> bytes:
    uid = bytes.fromhex(utterance_hex)
    if len(uid) != 16:
        raise ValueError("utterance id must be 16 bytes (32 hex chars)")
    return bytes([TTS_FRAME]) + uid + pcm16


def decode_binary(frame: bytes) -> tuple[int, str, bytes]:
    """Return (kind, utterance_hex, payload)."""
    if not frame:
        raise ValueError("empty binary frame")
    kind = frame[0]
    if kind == MIC_FRAME:
        return kind, "", frame[1:]
    if kind == TTS_FRAME:
        if len(frame) < 17:
            raise ValueError("short tts frame")
        return kind, frame[1:17].hex(), frame[17:]
    raise ValueError(f"unknown binary frame type {kind:#x}")


def resample_pcm16(pcm16: bytes, src_rate: int, dst_rate: int = TTS_SAMPLE_RATE) -> bytes:
    if src_rate == dst_rate or not pcm16:
        return pcm16
    import numpy as np

    x = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    n_out = int(len(x) * dst_rate / src_rate)
    idx = np.linspace(0, len(x) - 1, n_out)
    y = np.interp(idx, np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()
