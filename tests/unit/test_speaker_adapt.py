from __future__ import annotations

import numpy as np

from jarvis_core.voice.speaker_id import SpeakerVerifier, voiceprint_file


class FakeEncoder:
    def __init__(self, vec):
        self.vec = np.asarray(vec, dtype=np.float32)

    def embed(self, pcm):
        return self.vec / np.linalg.norm(self.vec)


def test_voiceprint_adapts_toward_clear_matches_and_is_saved():
    base = np.zeros(256, dtype=np.float32)
    base[0] = 1.0
    v = SpeakerVerifier(FakeEncoder(base))
    v.voiceprint = base.copy()
    drift = np.zeros(256, dtype=np.float32)
    drift[0], drift[1] = 0.8, 0.6  # how you sound through the earbuds
    drift = drift / np.linalg.norm(drift)
    before = float(v.voiceprint @ drift)
    for _ in range(10):
        v.adapt(drift)
    assert float(v.voiceprint @ drift) > before  # moved toward your real sound
    assert abs(np.linalg.norm(v.voiceprint) - 1.0) < 1e-4
    assert voiceprint_file().exists()  # persisted after a few updates
    sim, emb = v.check(b"\x00" * 64000)
    assert sim is not None and emb is not None
