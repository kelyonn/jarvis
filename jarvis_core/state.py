"""Daemon state machine (see plan section 3.2)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import Enum

log = logging.getLogger(__name__)


class State(str, Enum):
    BOOTING = "booting"
    IDLE = "idle"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    CONFIRMING = "confirming"
    SPEAKING = "speaking"
    FOLLOW_UP = "follow_up"
    ANNOUNCING = "announcing"
    PAUSED = "paused"
    RESUMING = "resuming"


S = State
_ACTIVE = {S.IDLE, S.LISTENING, S.TRANSCRIBING, S.THINKING, S.CONFIRMING, S.SPEAKING,
           S.FOLLOW_UP, S.ANNOUNCING}

ALLOWED: dict[State, set[State]] = {
    S.BOOTING: {S.IDLE, S.PAUSED},
    S.IDLE: {S.LISTENING, S.ANNOUNCING, S.THINKING, S.PAUSED},
    S.LISTENING: {S.TRANSCRIBING, S.IDLE, S.PAUSED, S.THINKING},  # THINKING: a typed request arrives mid-listen
    S.TRANSCRIBING: {S.THINKING, S.IDLE, S.PAUSED, S.LISTENING, S.FOLLOW_UP},
    S.THINKING: {S.SPEAKING, S.CONFIRMING, S.FOLLOW_UP, S.IDLE, S.PAUSED, S.LISTENING},
    S.CONFIRMING: {S.THINKING, S.SPEAKING, S.IDLE, S.PAUSED, S.LISTENING},
    S.SPEAKING: {S.FOLLOW_UP, S.LISTENING, S.CONFIRMING, S.IDLE, S.PAUSED, S.THINKING},
    S.FOLLOW_UP: {S.LISTENING, S.IDLE, S.PAUSED, S.ANNOUNCING, S.THINKING},  # THINKING: typed follow-up
    S.ANNOUNCING: {S.FOLLOW_UP, S.IDLE, S.LISTENING, S.PAUSED},
    S.PAUSED: {S.RESUMING},
    S.RESUMING: {S.ANNOUNCING, S.IDLE, S.PAUSED},
}


class InvalidTransition(RuntimeError):
    pass


class StateMachine:
    def __init__(
        self,
        *,
        strict: bool = False,
        on_change: Callable[[State, State], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.state = State.BOOTING
        self.strict = strict
        self._on_change = on_change
        self._clock = clock
        self.entered_at = clock()

    def go(self, new: State) -> bool:
        old = self.state
        if new == old:
            return False
        if new not in ALLOWED[old]:
            msg = f"invalid transition {old.value} -> {new.value}"
            if self.strict:
                raise InvalidTransition(msg)
            log.warning(msg)
        self.state = new
        self.entered_at = self._clock()
        if self._on_change:
            self._on_change(old, new)
        return True

    def time_in_state(self) -> float:
        return self._clock() - self.entered_at

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE

    @property
    def busy_with_turn(self) -> bool:
        return self.state in {S.LISTENING, S.TRANSCRIBING, S.THINKING, S.CONFIRMING, S.SPEAKING,
                              S.ANNOUNCING}
