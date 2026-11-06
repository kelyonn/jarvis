"""Speaker verification: act only on the owner's voice.

A small speech encoder (the GE2E model shipped with the ``resemblyzer`` wheel) turns audio into a 256-number voiceprint.
You enrol once by reading a few phrases; afterwards each utterance is compared with your voiceprint by cosine similarity.
Jarvis's own voice, a TV, or another person scores low and is ignored. Everything runs locally; the voiceprint is stored
at ``~/.jarvis/voiceprint.npy`` (mode 0600) and never leaves the machine.

Only the model weights are used from the wheel (loaded with ``weights_only=True``). Its audio helpers pull in ``librosa``
and ``webrtcvad``, so the mel-spectrogram (the same recipe librosa uses) is computed here with torch.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

import numpy as np

from jarvis_core import paths

log = logging.getLogger(__name__)

SR = 16000
N_FFT = 400  # 25 ms
HOP = 160  # 10 ms
N_MELS = 40
PARTIAL_FRAMES = 160  # 1.6 s windows, as the encoder was trained


def voiceprint_file() -> Path:
    return paths.home() / "voiceprint.npy"


def _mel_basis(sr: int = SR, n_fft: int = N_FFT, n_mels: int = N_MELS) -> np.ndarray:
    """librosa.filters.mel (Slaney scale and norm), written out so librosa is not needed."""

    def hz_to_mel(f):
        f = np.asanyarray(f, dtype=np.float64)
        mels = f / (200.0 / 3)
        return np.where(f >= 1000.0, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) / (np.log(6.4) / 27.0), mels)

    def mel_to_hz(m):
        m = np.asanyarray(m, dtype=np.float64)
        return np.where(m >= 15.0, 1000.0 * np.exp((np.log(6.4) / 27.0) * (m - 15.0)), (200.0 / 3) * m)

    fftfreqs = np.linspace(0, sr / 2, 1 + n_fft // 2)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(sr / 2), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)
    weights = np.zeros((n_mels, 1 + n_fft // 2))
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_f[2 : n_mels + 2] - mel_f[:n_mels])
    return (weights * enorm[:, None]).astype(np.float32)


class VoiceEncoder:
    """The GE2E LSTM from resemblyzer, loaded without importing the package (its imports need librosa/webrtcvad)."""

    def __init__(self) -> None:
        import torch
        from torch import nn

        spec = importlib.util.find_spec("resemblyzer")
        if spec is None or not spec.submodule_search_locations:
            raise RuntimeError("resemblyzer is not installed (pip install resemblyzer --no-deps)")
        weights = Path(list(spec.submodule_search_locations)[0]) / "pretrained.pt"
        self.torch = torch
        self.lstm = nn.LSTM(N_MELS, 256, num_layers=3, batch_first=True)
        self.linear = nn.Linear(256, 256)
        state = torch.load(weights, map_location="cpu", weights_only=True)["model_state"]
        self.lstm.load_state_dict({k[5:]: v for k, v in state.items() if k.startswith("lstm.")})
        self.linear.load_state_dict({k[7:]: v for k, v in state.items() if k.startswith("linear.")})
        self.lstm.eval()
        self.linear.eval()
        self._basis = torch.from_numpy(_mel_basis())
        self._window = torch.hann_window(N_FFT, periodic=True)

    def _mels(self, wav: np.ndarray):
        t = self.torch
        x = t.from_numpy(wav.astype(np.float32))
        spec = t.stft(x, n_fft=N_FFT, hop_length=HOP, window=self._window, center=True, pad_mode="constant",
                      return_complex=True)
        power = spec.abs() ** 2
        return (self._basis @ power).T  # (frames, 40)

    def embed(self, pcm16: bytes) -> np.ndarray | None:
        wav = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        if wav.size < SR * 0.4:
            return None
        rms = float(np.sqrt(np.mean(wav**2)))
        if rms < 1e-4:
            return None
        wav = np.clip(wav * (10 ** (-30 / 20) / rms), -1.0, 1.0)  # loudness to -30 dBFS like the training data
        with self.torch.no_grad():
            mels = self._mels(wav)
            n = mels.shape[0]
            if n < PARTIAL_FRAMES:
                reps = int(np.ceil(PARTIAL_FRAMES / n))
                mels = mels.repeat(reps, 1)[:PARTIAL_FRAMES]
                n = PARTIAL_FRAMES
            starts = list(range(0, n - PARTIAL_FRAMES + 1, PARTIAL_FRAMES // 2)) or [0]
            batch = self.torch.stack([mels[s : s + PARTIAL_FRAMES] for s in starts])
            _, (hidden, _) = self.lstm(batch)
            partial = self.torch.relu(self.linear(hidden[-1]))
            partial = partial / self.torch.linalg.norm(partial, dim=1, keepdim=True)
            emb = partial.mean(dim=0)
            emb = emb / self.torch.linalg.norm(emb)
        return emb.numpy()


class SpeakerVerifier:
    def __init__(self, encoder: VoiceEncoder | None = None) -> None:
        self.encoder = encoder or VoiceEncoder()
        self.voiceprint: np.ndarray | None = None
        self.load()

    @property
    def enrolled(self) -> bool:
        return self.voiceprint is not None

    def load(self) -> None:
        f = voiceprint_file()
        if f.exists():
            try:
                vp = np.load(f, allow_pickle=False)
                if vp.shape == (256,):
                    self.voiceprint = vp.astype(np.float32)
            except Exception:  # noqa: BLE001
                log.warning("could not read the voiceprint; verification is off", exc_info=True)

    def enroll(self, samples: list[bytes]) -> int:
        """Average the voiceprints of the good samples; returns how many were used."""
        embs = [e for e in (self.encoder.embed(s) for s in samples) if e is not None]
        if len(embs) < 3:
            raise ValueError("need at least three clear phrases")
        mean = np.mean(embs, axis=0)
        mean = mean / np.linalg.norm(mean)
        fd = os.open(voiceprint_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            np.save(fh, mean.astype(np.float32))
        self.voiceprint = mean.astype(np.float32)
        return len(embs)

    def forget(self) -> None:
        voiceprint_file().unlink(missing_ok=True)
        self.voiceprint = None

    def check(self, pcm16: bytes) -> tuple[float | None, np.ndarray | None]:
        """(similarity, embedding) so a good match can also be used to refine the voiceprint."""
        if self.voiceprint is None:
            return None, None
        emb = self.encoder.embed(pcm16)
        if emb is None:
            return None, None
        return float(np.dot(emb, self.voiceprint)), emb

    def adapt(self, emb: np.ndarray, weight: float = 0.05) -> None:
        """Blend a clearly-yours utterance into the voiceprint (a slow moving average), saved every few updates."""
        if self.voiceprint is None:
            return
        vp = (1 - weight) * self.voiceprint + weight * emb
        self.voiceprint = (vp / np.linalg.norm(vp)).astype(np.float32)
        self._adapted = getattr(self, "_adapted", 0) + 1
        if self._adapted % 5 == 0:
            try:
                fd = os.open(voiceprint_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    np.save(fh, self.voiceprint)
            except OSError:
                log.warning("could not save the refined voiceprint", exc_info=True)

    def similarity(self, pcm16: bytes) -> float | None:
        """Cosine similarity to the owner's voiceprint (about 0.75 and up for the owner), or None if it cannot be judged."""
        if self.voiceprint is None:
            return None
        emb = self.encoder.embed(pcm16)
        if emb is None:
            return None
        return float(np.dot(emb, self.voiceprint))
