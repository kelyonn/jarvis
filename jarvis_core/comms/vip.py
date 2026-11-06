"""The VIP list: people whose messages Jarvis announces immediately."""

from __future__ import annotations

import json
import re
from pathlib import Path

from jarvis_core import paths
from jarvis_core.config import Config, load_config


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w@+.\s]", "", s.lower())).strip()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


class VipList:
    """Names come from ``[vip] contacts`` in config.toml plus a JSON file edited by voice or CLI."""

    def __init__(self, cfg: Config, path: Path | None = None) -> None:
        self.cfg = cfg
        self.path = path or paths.home() / "vip.json"

    # -- persistence -----------------------------------------------------------------------------
    def _stored(self) -> list[str]:
        try:
            data = json.loads(self.path.read_text())
            return [str(x) for x in data] if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _write(self, names: list[str]) -> None:
        self.path.write_text(json.dumps(sorted(set(names), key=str.lower), indent=2))

    def names(self) -> list[str]:
        seen: dict[str, str] = {}
        for n in [*self.cfg.vip.contacts, *self._stored()]:
            seen.setdefault(_norm(n), n)
        return sorted(seen.values(), key=str.lower)

    def add(self, name: str) -> bool:
        name = name.strip()
        if not name or _norm(name) in {_norm(n) for n in self.names()}:
            return False
        self._write([*self._stored(), name])
        return True

    def remove(self, name: str) -> bool:
        key = _norm(name)
        stored = self._stored()
        keep = [n for n in stored if _norm(n) != key]
        if len(keep) == len(stored):
            return False
        self._write(keep)
        return True

    # -- matching --------------------------------------------------------------------------------
    def is_vip(self, sender: str, ids: list[str] | None = None) -> bool:
        """``sender`` is a display name; ``ids`` are phone numbers / emails / handles for the same person."""
        s_norm = _norm(sender.split(" in ")[0]) if sender else ""
        s_tokens = set(s_norm.split())
        id_norms = [_norm(i) for i in (ids or []) if i]
        id_digits = [_digits(i)[-10:] for i in (ids or []) if len(_digits(i)) >= 7]
        for vip in self.names():
            v = _norm(vip)
            if not v:
                continue
            if v == s_norm or v in id_norms:
                return True
            v_digits = _digits(vip)
            if len(v_digits) >= 7 and v_digits[-10:] in id_digits:
                return True
            v_tokens = set(v.split())
            if v_tokens and not v_digits and v_tokens <= s_tokens:
                return True
        return False


def vip_cli(action: str, name: str | None) -> int:
    vip = VipList(load_config())
    if action == "list":
        for n in vip.names():
            print(n)
        return 0
    if not name:
        print("name required")
        return 2
    if action == "add":
        print(f"added {name}" if vip.add(name) else f"{name} is already a VIP")
        return 0
    ok = vip.remove(name)
    print(f"removed {name}" if ok else f"{name} isn't in the stored VIP list (names in config.toml must be edited there)")
    return 0 if ok else 1
