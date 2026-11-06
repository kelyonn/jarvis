"""Shared environment for collectors: HTTP, cache, clock, location."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from jarvis_core.briefing.store import Store
from jarvis_core.config import Config
from jarvis_core.skills.base import ToolContext

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) JarvisAssistant/0.1"


@dataclass
class Env:
    cfg: Config
    http: httpx.AsyncClient
    store: Store
    ctx: ToolContext | None = None
    now: Callable[[], datetime] = lambda: datetime.now().astimezone()
    services: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0), headers={"User-Agent": UA}, follow_redirects=True)

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, cache_key: str | None = None, ttl_s: float = 600.0
    ) -> tuple[Any, bool]:
        """GET JSON with a short-lived cache and stale fallback. Returns (data, stale)."""
        if cache_key:
            hit = self.store.cache_get(cache_key, max_age_s=ttl_s)
            if hit:
                return hit[0], False
        try:
            r = await self.http.get(url, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if cache_key:
                stale = self.store.cache_get(cache_key)
                if stale:
                    log.warning("using stale cache for %s (%s)", cache_key, e)
                    return stale[0], True
            raise
        if cache_key:
            self.store.cache_put(cache_key, data)
        return data, False

    async def get_text(self, url: str, *, cache_key: str | None = None, ttl_s: float = 300.0) -> tuple[str, bool]:
        if cache_key:
            hit = self.store.cache_get(cache_key, max_age_s=ttl_s)
            if hit:
                return hit[0], False
        try:
            r = await self.http.get(url)
            r.raise_for_status()
            text = r.text
        except Exception:
            if cache_key:
                stale = self.store.cache_get(cache_key)
                if stale:
                    return stale[0], True
            raise
        if cache_key:
            self.store.cache_put(cache_key, text)
        return text, False


@dataclass
class Place:
    city: str
    lat: float
    lon: float
    timezone: str = ""


async def resolve_place(env: Env) -> Place:
    """Where is the user? Config override > JarvisShell (CoreLocation) > IP lookup > timezone guess."""
    cfg = env.cfg
    if cfg.devices.location_city:
        p = await geocode(env, cfg.devices.location_city)
        if p:
            return p
    cached = env.store.cache_get("place", max_age_s=6 * 3600)
    if cached:
        return Place(**cached[0])
    place: Place | None = None
    if env.ctx and env.ctx.frontend is not None and getattr(env.ctx.frontend, "connected", False):
        try:
            loc = await env.ctx.frontend.request("location", {}, timeout=8)
            if loc.get("lat") is not None:
                place = Place(loc.get("city") or "your location", float(loc["lat"]), float(loc["lon"]), loc.get("timezone", ""))
        except Exception as e:
            log.info("shell location unavailable: %s", e)
    if place is None:
        try:
            data, _ = await env.get_json("https://ipwho.is/", cache_key=None)
            if data.get("success", True) and data.get("latitude") is not None:
                place = Place(data.get("city") or "your area", float(data["latitude"]), float(data["longitude"]),
                              (data.get("timezone") or {}).get("id", ""))
        except Exception as e:
            log.info("ip geolocation failed: %s", e)
    if place is None:
        guess = env.cfg.user.timezone.split("/")[-1].replace("_", " ")
        place = await geocode(env, guess)
    if place is None:
        raise RuntimeError("could not determine location")
    env.store.cache_put("place", place.__dict__)
    return place


async def geocode(env: Env, name: str) -> Place | None:
    try:
        data, _ = await env.get_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 1, "language": "en"},
            cache_key=f"geo:{name.lower()}",
            ttl_s=30 * 86400,
        )
    except Exception:
        return None
    res = (data.get("results") or [None])[0]
    if not res:
        return None
    return Place(res.get("name", name), float(res["latitude"]), float(res["longitude"]), res.get("timezone", ""))


async def gather_limited(coros: dict[str, Any], timeout_s: float = 10.0) -> dict[str, Any]:
    """Run named awaitables concurrently; each result is the value or the raised exception."""

    async def one(c: Any) -> Any:
        try:
            return await asyncio.wait_for(c, timeout_s)
        except BaseException as e:  # noqa: BLE001 - isolated per collector
            if isinstance(e, asyncio.CancelledError):
                raise
            return e

    keys = list(coros)
    results = await asyncio.gather(*(one(coros[k]) for k in keys))
    return dict(zip(keys, results))
