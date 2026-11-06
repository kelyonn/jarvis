"""The evening look-ahead: tomorrow's first meeting and the weather overnight and tomorrow."""

from __future__ import annotations

from dataclasses import dataclass

from jarvis_core.briefing.collectors import calendar as cal_c
from jarvis_core.briefing.collectors import weather as wx_c
from jarvis_core.briefing.env import Env
from jarvis_core.briefing.models import CalEvent, Weather


@dataclass
class Tomorrow:
    first: CalEvent | None
    count: int
    weather: Weather | None


async def collect(env: Env) -> Tomorrow:
    events = [e for e in await cal_c.collect(env, 1) if not e.all_day]
    try:
        w = await wx_c.collect(env)
    except Exception:  # noqa: BLE001 - the meetings are still worth saying
        w = None
    return Tomorrow(events[0] if events else None, len(events), w)


def describe(t: Tomorrow) -> str:
    parts = []
    if t.first:
        when = cal_c.spoken_time(t.first.start.astimezone())
        more = f", out of {t.count} meetings" if t.count > 1 else ""
        parts.append(f"Tomorrow your first meeting is {t.first.title} at {when}{more}.")
    else:
        parts.append("Nothing is scheduled for tomorrow.")
    w = t.weather
    if w is not None and w.tomorrow_low_c is not None:
        s = f"Overnight it drops to {w.tomorrow_low_c:.0f}"
        if w.tomorrow_high_c is not None:
            s += f", and tomorrow reaches {w.tomorrow_high_c:.0f} with {w.tomorrow_condition}"
        s += "."
        if w.tomorrow_rain_pct >= 40:
            s += f" There's a {w.tomorrow_rain_pct} percent chance of rain."
        parts.append(s)
    return " ".join(parts)
