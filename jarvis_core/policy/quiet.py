"""Quiet hours (may wrap past midnight)."""

from __future__ import annotations

from datetime import datetime

from jarvis_core.config import parse_hhmm


def in_quiet_hours(window: list[str], now: datetime) -> bool:
    (sh, sm), (eh, em) = parse_hhmm(window[0]), parse_hhmm(window[1])
    cur = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)
