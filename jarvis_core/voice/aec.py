"""Echo cancellation, so Jarvis can be talked over on speakers without hearing itself.

Jarvis knows exactly what it is playing. The microphone hears that sound (changed by the room and the speaker) plus you.
This learns the room's effect on the sound and subtracts it, leaving you.

How it works: a partitioned-block frequency-domain adaptive filter (the standard "PBFDAF" design). The reference (what
we play) is split into 20 ms blocks; the filter holds one frequency response per block of history, together covering
``partitions * 20 ms`` of delay and room reverb. Each frame it predicts the echo from the recent reference, subtracts it,
and nudges the filter to predict better. Adaptation is paused when you are likely speaking over it, so your voice is not
learned as "echo".

Honest limits: this is tuned on simulated rooms, not on your Mac. Playback delay beyond the filter length (400 ms by
default) cannot be cancelled. A loud, nonlinear speaker leaves residual echo that this does not remove. It therefore
reports how much echo it is removing (``erle_db``) and the daemon only trusts it for voice interruption when that is high.
"""

from __future__ import annotations

from collections import deque

import numpy as np

FRAME = 320  # samples per block: 20 ms at 16 kHz
RATE = 16000
EPS = 1e-8


def resample_to_16k(pcm16: bytes, rate: int) -> np.ndarray:
    """int16 bytes at any rate to float32 samples at 16 kHz (linear interpolation: enough for a reference signal)."""
    x = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype=np.int16).astype(np.float32)
    if rate == RATE or x.size == 0:
        return x
    n = int(round(x.size * RATE / rate))
    return np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)


class ReferenceTimeline:
    """What the speaker is playing right now, as the filter's reference.

    Audio is handed to the Shell faster than it is played, so it is queued here and consumed one frame per microphone
    frame, which is the speed it actually plays at. ``stop()`` empties it when playback is cut off.
    """

    def __init__(self, max_seconds: float = 120.0) -> None:
        self._q: deque[np.ndarray] = deque()
        self._n = 0
        self._max = int(max_seconds * RATE)

    def push(self, pcm16: bytes, rate: int) -> None:
        x = resample_to_16k(pcm16, rate)
        if x.size == 0:
            return
        self._q.append(x)
        self._n += x.size
        while self._n > self._max and self._q:  # runaway queue: drop the oldest rather than grow forever
            self._n -= self._q.popleft().size

    def next(self, n: int = FRAME) -> np.ndarray:
        """The next ``n`` samples of playback (silence if nothing is playing)."""
        out = np.zeros(n, dtype=np.float32)
        filled = 0
        while filled < n and self._q:
            head = self._q[0]
            take = min(n - filled, head.size)
            out[filled:filled + take] = head[:take]
            filled += take
            if take == head.size:
                self._q.popleft()
            else:
                self._q[0] = head[take:]
            self._n -= take
        return out

    def stop(self) -> None:
        self._q.clear()
        self._n = 0

    @property
    def playing(self) -> bool:
        return self._n > 0


class EchoCanceller:
    def __init__(self, partitions: int = 20, step: float = 0.6, frame: int = FRAME) -> None:
        self.B = frame
        self.P = partitions
        self.mu = step
        bins = frame + 1  # rfft of 2B samples
        self._W = np.zeros((partitions, bins), dtype=np.complex64)
        self._X = np.zeros((partitions, bins), dtype=np.complex64)
        self._prev_ref = np.zeros(frame, dtype=np.float32)
        self._power = np.full(bins, 1e3, dtype=np.float32)
        self._frames = 0
        self._tail = 0
        # How much echo is being removed, smoothed (energy of mic vs energy left after cancelling), in frames with playback.
        self._e_mic = 1e-3
        self._e_res = 1e-3
        self._ratio_slow = 1.0
        self.frozen = 0  # frames where adaptation paused because you were probably talking

    # ------------------------------------------------------------------ public

    @property
    def erle_db(self) -> float:
        """Echo return loss enhancement: how many dB quieter the echo is after cancelling. 0 until it has learned."""
        return float(max(0.0, 10 * np.log10(self._e_mic / max(self._e_res, 1e-3))))

    def reset(self) -> None:
        self.__init__(self.P, self.mu, self.B)

    def process(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        """One block: ``mic`` and ``ref`` are float32 arrays of ``frame`` samples. Returns the cleaned block."""
        B = self.B
        if mic.size != B or ref.size != B:
            raise ValueError(f"blocks must be {B} samples")
        # With nothing playing there is no echo: pass the audio through untouched and let the filter rest.
        ref_energy = float(np.dot(ref, ref))
        if ref_energy >= 1.0:
            self._tail = self.P + 1  # keep working while the last of the reference is still echoing around the room
        elif self._tail > 0:
            self._tail -= 1
        else:
            self._prev_ref = ref
            if self._X.any():
                self._X[:] = 0  # old reference must not be mistaken for the start of the next playback
            return mic
        x_block = np.concatenate([self._prev_ref, ref])
        self._prev_ref = ref
        self._X = np.roll(self._X, 1, axis=0)
        self._X[0] = np.fft.rfft(x_block).astype(np.complex64)

        y_hat = np.fft.irfft(np.sum(self._W * self._X, axis=0), n=2 * B)[B:].astype(np.float32)
        err = mic - y_hat

        mic_e, err_e = float(np.dot(mic, mic)), float(np.dot(err, err))
        playing = ref_energy > 1e3 or float(np.sum(np.abs(self._X[1:]))) > 0
        # Adapt unless you seem to be talking: once the filter has converged, a sudden jump in what is left over means
        # something new (your voice) is in the microphone, and learning from it would teach the filter to cancel you.
        ratio = err_e / (mic_e + EPS)
        converged = self._ratio_slow < 0.35 and self._frames > 50
        double_talk = converged and ratio > 0.7 and mic_e > 1e4
        if playing:
            self._ratio_slow = 0.97 * self._ratio_slow + 0.03 * min(ratio, 1.0) if not double_talk else self._ratio_slow
            if not double_talk:
                self._e_mic = 0.98 * self._e_mic + 0.02 * mic_e
                self._e_res = 0.98 * self._e_res + 0.02 * err_e
        if double_talk:
            self.frozen += 1
        elif playing:
            self._adapt(err)
        self._frames += 1
        return err

    # ----------------------------------------------------------------- internal

    def _adapt(self, err: np.ndarray) -> None:
        B = self.B
        E = np.fft.rfft(np.concatenate([np.zeros(B, dtype=np.float32), err])).astype(np.complex64)
        inst = np.sum(np.abs(self._X) ** 2, axis=0).astype(np.float32)
        self._power = 0.9 * self._power + 0.1 * inst
        norm = self.mu / (self._power + EPS * self.P + 1.0)
        update = np.conj(self._X) * (E * norm)[None, :]
        # Constrain each partition's response to a causal impulse of one block: zero the second half in the time domain.
        upd_t = np.fft.irfft(update, n=2 * B, axis=1)
        upd_t[:, B:] = 0
        self._W += np.fft.rfft(upd_t, axis=1).astype(np.complex64)


def process_frame(aec: EchoCanceller, timeline: ReferenceTimeline, pcm16: bytes) -> bytes:
    """Clean one microphone frame of 16-bit audio using whatever is currently playing. Other sizes pass through."""
    if len(pcm16) != FRAME * 2:
        return pcm16
    mic = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
    out = aec.process(mic, timeline.next(FRAME))
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()
