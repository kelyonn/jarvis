"""``jarvisd say``: speak text through the Jarvis voice (no daemon needed)."""

from __future__ import annotations

import asyncio

import numpy as np

from jarvis_core.config import load_config
from jarvis_core.voice.text_norm import speakable
from jarvis_core.voice.tts import build_tts


async def say_text(text: str) -> int:
    import sounddevice as sd

    cfg = load_config()
    tts = build_tts(cfg.voice)
    loop = asyncio.get_running_loop()
    chunk = await loop.run_in_executor(None, tts.synth, speakable(text))
    sd.play(np.frombuffer(chunk.pcm16, dtype=np.int16), chunk.sample_rate)
    sd.wait()
    return 0
