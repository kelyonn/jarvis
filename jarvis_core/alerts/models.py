from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


@dataclass
class Alert:
    key: str  # stable id used for de-duplication
    kind: str  # meeting | vip | market | rain | news | battery
    title: str  # short text for the orb queue
    speech: str  # what Jarvis says
    priority: str = "normal"  # urgent | high | normal | low
    created: float = field(default_factory=time.time)
    expires: float | None = None  # epoch seconds after which it is no longer worth saying
    follow_up: bool = False  # open the follow-up window so the user can answer ("shall I draft a reply?")
    payload: dict[str, Any] = field(default_factory=dict)

    def expired(self, now: float | None = None) -> bool:
        return self.expires is not None and (now or time.time()) >= self.expires

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(d: dict[str, Any]) -> Alert:
        return Alert(**d)
