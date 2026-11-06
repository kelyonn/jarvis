"""``jarvisd alerts``: dry-run every alert rule and show the state of Focus / calls."""

from __future__ import annotations

import asyncio

from jarvis_core.alerts.rules import default_rules
from jarvis_core.briefing.cli import build_service
from jarvis_core.config import load_config
from jarvis_core.policy.presence import Presence


async def run_alerts_check() -> int:
    cfg = load_config()
    _svc, env = build_service(cfg)
    try:
        presence = Presence()
        focus, name = presence.focus()
        label = "unknown (grant Full Disk Access to read Focus state)" if focus is None else ("on: " + (name or "yes") if focus else "off")
        print(f"focus: {label}; quiet hours: {cfg.user.quiet_hours[0]}-{cfg.user.quiet_hours[1]}")
        rules = default_rules()
        results = await asyncio.gather(*(asyncio.wait_for(r.check(env), 30) for r in rules), return_exceptions=True)
        for rule, res in zip(rules, results, strict=True):
            if isinstance(res, Exception):
                print(f"- {rule.kind}: unavailable ({res!r})")
            elif not res:
                print(f"- {rule.kind}: nothing to report")
            else:
                for a in res:
                    print(f"- {rule.kind} [{a.priority}]: {a.speech}")
        return 0
    finally:
        await env.http.aclose()
