"""Generated earcons (short chimes); no audio assets needed."""

from __future__ import annotations

import numpy as np

SR = 24000

_SPECS: dict[str, list[tuple[float, float]]] = {
    # name -> [(frequency_hz, duration_s), ...]
    "listen": [(880.0, 0.07), (1318.5, 0.09)],
    "done": [(1318.5, 0.06), (880.0, 0.08)],
    "error": [(330.0, 0.12), (247.0, 0.18)],
    "alert": [(988.0, 0.08), (988.0, 0.08), (1319.0, 0.12)],
    "pardon": [(740.0, 0.1)],
}


def make_earcon(name: str, volume: float = 0.25) -> bytes:
    """PCM16 mono 24 kHz bytes for the named earcon."""
    spec = _SPECS.get(name, _SPECS["done"])
    out = []
    for freq, dur in spec:
        t = np.linspace(0, dur, int(SR * dur), endpoint=False)
        env = np.minimum(1.0, np.minimum(t / 0.01, (dur - t) / 0.02))
        out.append(np.sin(2 * np.pi * freq * t) * env)
        out.append(np.zeros(int(SR * 0.015)))
    wave_ = np.concatenate(out) * volume
    return (np.clip(wave_, -1, 1) * 32767).astype(np.int16).tobytes()


EARCON_NAMES = tuple(_SPECS)
