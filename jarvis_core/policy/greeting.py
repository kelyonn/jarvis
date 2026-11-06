"""What Jarvis says when you open the lid, shaped by where you are in your day (default 09:00 to 03:00)."""

from __future__ import annotations

import random
from datetime import datetime

from jarvis_core.config import Config


def day_phase(cfg: Config, now: datetime) -> str:
    """early (after your day ended, before it starts), morning, afternoon, evening or late (the last hours before it ends)."""
    h, start, end = now.hour, cfg.user.day_start_hour, cfg.user.day_end_hour
    if start != end:
        awake = start <= h < end if start < end else (h >= start or h < end)
        if not awake:
            return "early"
    if h >= 22 or h < end:
        return "late"
    return "morning" if h < 12 else "afternoon" if h < 17 else "evening"


def welcome_line(cfg: Config, now: datetime, *, first_of_day: bool, rng: random.Random | None = None) -> str:
    pick = (rng or random).choice
    name = cfg.user.name or "sir"
    phase = day_phase(cfg, now)
    if first_of_day:
        opener = {
            "early": f"You're up early, {name}.",
            "morning": f"Good morning, {name}.",
            "afternoon": f"Good afternoon, {name}.",
            "evening": f"Good evening, {name}.",
            "late": f"Good evening, {name}.",
        }[phase]
        return f"{opener} Welcome back. Ready when you are."
    return pick({
        "early": [f"Welcome back, {name}. Early start.", f"Welcome back, {name}. Still up, or up already?"],
        "morning": [f"Welcome back, {name}.", f"Good to have you back, {name}."],
        "afternoon": [f"Welcome back, {name}.", f"Good to have you back, {name}. Ready when you are."],
        "evening": [f"Welcome back, {name}.", f"Welcome back, {name}. Ready when you are."],
        "late": [f"Welcome back, {name}. Burning the midnight oil?", f"Welcome back, {name}. It's getting late."],
    }[phase])
