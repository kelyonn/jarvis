"""Plain data carried between collectors, composer and scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Weather:
    city: str
    temp_c: float
    feels_c: float
    condition: str
    high_c: float
    low_c: float
    rain_chance_pct: int  # max probability over the rest of today
    rain_next_hours: list[tuple[int, int]] = field(default_factory=list)  # (hour_offset, probability)
    wind_kmh: float = 0.0
    sunset: str = ""  # HH:MM local
    tomorrow_high_c: float | None = None
    tomorrow_low_c: float | None = None
    tomorrow_condition: str = ""
    tomorrow_rain_pct: int = 0
    uv_max: float | None = None  # today's peak UV index
    aqi: int | None = None  # US air quality index right now

    def raining_soon(self, hours: int, threshold: int) -> int | None:
        """Hour offset of the first slot within ``hours`` at or above ``threshold`` % chance, else None."""
        for off, p in self.rain_next_hours:
            if off <= hours and p >= threshold:
                return off
        return None


@dataclass
class Quote:
    symbol: str
    name: str
    price: float
    prev_close: float
    currency: str = ""
    market_state: str = ""

    @property
    def change_pct(self) -> float:
        return 0.0 if not self.prev_close else (self.price - self.prev_close) / self.prev_close * 100


@dataclass
class Headline:
    title: str
    source: str
    published: datetime | None = None
    link: str = ""
    summary: str = ""


@dataclass
class CalEvent:
    id: str
    title: str
    start: datetime
    end: datetime | None = None
    location: str = ""
    all_day: bool = False
    attendees: int = 0


@dataclass
class MailItem:
    id: str
    sender: str
    subject: str
    snippet: str = ""
    important: bool = False
    vip: bool = False
    timestamp: datetime | None = None


@dataclass
class SectionResult:
    key: str
    ok: bool = True
    data: Any = None
    error: str = ""
    stale: bool = False


@dataclass
class BriefingData:
    generated_at: datetime
    mode: str  # "full" | "delta" | "slot"
    sections: dict[str, SectionResult] = field(default_factory=dict)
    since: datetime | None = None
    notes: list[str] = field(default_factory=list)

    def get(self, key: str) -> Any:
        r = self.sections.get(key)
        return r.data if r and r.ok else None

    def failed(self) -> list[str]:
        return [k for k, r in self.sections.items() if not r.ok]
