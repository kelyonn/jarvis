"""Spotify: the user's own library (playlists, including ones inside folders, liked songs) and playback.

Catalogue search works with just a client id/secret (see ``media._spotify_token``). Private playlists, and queueing
or reading the queue, need the user to sign in once (``jarvisd spotify-login``): the refresh token goes to the
Keychain, never to disk or logs. The Web API does not expose folders, but it lists every playlist flat, so a
playlist inside a folder is found by its name. Queueing needs ``user-modify-playback-state``: anyone who signed in
before that scope was added needs to run ``jarvisd spotify-login`` again.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from typing import Any

import httpx

from jarvis_core import secrets
from jarvis_core.errors import SkillError
from jarvis_core.skills.base import B, E, S, Tool, ToolContext, ToolRegistry, obj

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("playlist-read-private playlist-read-collaborative user-library-read "
          "user-modify-playback-state user-read-playback-state")
_cache: dict[str, Any] = {"token": None, "exp": 0.0, "playlists": None, "playlists_at": 0.0}


def _client() -> tuple[str, str]:
    cid, sec = secrets.get_secret("SPOTIFY_CLIENT_ID"), secrets.get_secret("SPOTIFY_CLIENT_SECRET")
    if not (cid and sec):
        raise SkillError("Spotify isn't set up, sir. Run jarvisd spotify-setup first.")
    return cid, sec


def auth_url(client_id: str, state: str) -> str:
    q = urllib.parse.urlencode({"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
                                "scope": SCOPES, "state": state})
    return f"https://accounts.spotify.com/authorize?{q}"


async def exchange_code(code: str) -> str:
    """Trade the authorisation code for tokens; stores the refresh token in the Keychain."""
    cid, sec = _client()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://accounts.spotify.com/api/token", auth=(cid, sec),
                         data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI})
    if r.status_code != 200:
        raise SkillError(f"Spotify sign-in failed ({r.status_code}).")
    d = r.json()
    if d.get("refresh_token"):
        secrets.set_secret("SPOTIFY_REFRESH_TOKEN", d["refresh_token"])
    _cache.update(token=d["access_token"], exp=time.time() + int(d.get("expires_in", 3600)) - 60, playlists=None)
    return d["access_token"]


async def user_token() -> str:
    if _cache["token"] and time.time() < _cache["exp"]:
        return _cache["token"]
    refresh = secrets.get_secret("SPOTIFY_REFRESH_TOKEN")
    if not refresh:
        raise SkillError("I need access to your Spotify library first, sir. Run jarvisd spotify-login once.")
    cid, sec = _client()
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post("https://accounts.spotify.com/api/token", auth=(cid, sec),
                         data={"grant_type": "refresh_token", "refresh_token": refresh})
    if r.status_code != 200:
        raise SkillError("Spotify no longer accepts my sign-in, sir. Run jarvisd spotify-login again.")
    d = r.json()
    if d.get("refresh_token"):
        secrets.set_secret("SPOTIFY_REFRESH_TOKEN", d["refresh_token"])
    _cache.update(token=d["access_token"], exp=time.time() + int(d.get("expires_in", 3600)) - 60)
    return d["access_token"]


async def all_playlists(*, fresh: bool = False) -> list[dict[str, Any]]:
    if _cache["playlists"] is not None and not fresh and time.time() - _cache["playlists_at"] < 300:
        return _cache["playlists"]
    token = await user_token()
    out: list[dict[str, Any]] = []
    url: str | None = "https://api.spotify.com/v1/me/playlists?limit=50"
    async with httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {token}"}) as c:
        while url and len(out) < 1500:
            r = await c.get(url)
            if r.status_code == 401:
                _cache["token"] = None
                raise SkillError("Spotify needs you to sign in again, sir. Run jarvisd spotify-login.")
            if r.status_code != 200:
                raise SkillError(f"Spotify wouldn't list your playlists ({r.status_code}).")
            d = r.json()
            out += [p for p in d.get("items", []) if p]
            url = d.get("next")
    _cache.update(playlists=out, playlists_at=time.time())
    return out


def _norm(s: str) -> str:
    s = re.sub(r"\b(?:my|the|playlist|folder|in|from|on|spotify)\b", " ", s.lower())
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def best_playlists(query: str, playlists: list[dict[str, Any]], limit: int = 3) -> list[tuple[int, dict[str, Any]]]:
    from rapidfuzz import fuzz

    q = _norm(query)
    scored = []
    for p in playlists:
        name = _norm(p.get("name", ""))
        if not name:
            continue
        score = max(fuzz.WRatio(q, name), 100 if q and q in name else 0)
        scored.append((int(score), p))
    scored.sort(key=lambda t: -t[0])
    return [t for t in scored[:limit] if t[0] >= 72]


async def _ensure_running(ctx: ToolContext) -> None:
    from jarvis_core.skills.media import _ensure_spotify

    await _ensure_spotify(ctx)


async def _play_playlist(ctx: ToolContext, a: dict[str, Any]) -> str:
    name = (a.get("name") or "").strip()
    if not name:
        raise SkillError("Which playlist, sir?")
    hits = best_playlists(name, await all_playlists())
    if not hits:
        hits = best_playlists(name, await all_playlists(fresh=True))  # maybe it was created in the last few minutes
    if not hits:
        raise SkillError(f"I couldn't find a playlist called {name} in your library, sir.")
    score, top = hits[0]
    await _ensure_running(ctx)
    await ctx.osa('on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run', top["uri"])
    if a.get("shuffle"):
        await ctx.osa('tell application "Spotify" to set shuffling to true')
    extra = ""
    if len(hits) > 1 and hits[1][0] >= score - 4 and hits[1][1]["name"] != top["name"]:
        extra = f" You also have one called {hits[1][1]['name']}."
    return f"Playing your playlist {top['name']}.{extra}"


async def _list_playlists(ctx: ToolContext, a: dict[str, Any]) -> str:
    pls = await all_playlists(fresh=True)
    q = (a.get("filter") or "").strip()
    if q:
        pls = [p for _, p in best_playlists(q, pls, limit=10)]
    if not pls:
        return "You have no matching playlists, sir."
    names = [p["name"] for p in pls[:12]]
    more = f", and {len(pls) - 12} more" if len(pls) > 12 else ""
    return f"You have {len(pls)}: " + ", ".join(names) + more + "."


async def _play_liked(ctx: ToolContext, a: dict[str, Any]) -> str:
    # The liked-songs collection is addressed through the user's own id: spotify:user:<id>:collection
    token = await user_token()
    async with httpx.AsyncClient(timeout=15, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.get("https://api.spotify.com/v1/me")
    if r.status_code != 200 or not r.json().get("id"):
        raise SkillError("I couldn't read your Spotify profile, sir.")
    await _ensure_running(ctx)
    await ctx.osa('on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run',
                  f"spotify:user:{r.json()['id']}:collection")
    await asyncio.sleep(1.2)
    now = await ctx.osa('tell application "Spotify"\nif player state is playing then return name of current track\nreturn ""\nend tell')
    if not now:
        raise SkillError("Spotify wouldn't start your liked songs, sir.")
    return "Playing your liked songs."


async def _queue_song(ctx: ToolContext, a: dict[str, Any]) -> str:
    from jarvis_core.skills.media import _spotify_find

    q = (a.get("query") or "").strip()
    if not q:
        raise SkillError("What should I queue, sir?")
    await _ensure_running(ctx)
    token = await user_token()
    found = await _spotify_find(token, q)
    if not found:
        raise SkillError(f"I couldn't find {q} on Spotify, sir.")
    uri, spoken = found
    async with httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.post("https://api.spotify.com/v1/me/player/queue", params={"uri": uri})
    if r.status_code == 403:
        raise SkillError("Spotify needs you to sign in again for this, sir. Run jarvisd spotify-login.")
    if r.status_code == 404:
        raise SkillError("Spotify needs to already be playing before I can queue anything, sir.")
    if r.status_code not in (200, 204):
        raise SkillError(f"Spotify wouldn't queue that, sir ({r.status_code}).")
    return f"Queued {spoken}."


async def _list_queue(ctx: ToolContext, a: dict[str, Any]) -> str:
    token = await user_token()
    async with httpx.AsyncClient(timeout=10, headers={"Authorization": f"Bearer {token}"}) as c:
        r = await c.get("https://api.spotify.com/v1/me/player/queue")
    if r.status_code != 200:
        raise SkillError("I couldn't read your Spotify queue, sir.")
    items = r.json().get("queue") or []
    if not items:
        return "Nothing queued, sir."
    names = [f"{t['name']} by {t['artists'][0]['name']}" for t in items[:5]]
    return "Up next: " + ", ".join(names) + "."


async def _set_shuffle(ctx: ToolContext, a: dict[str, Any]) -> str:
    await _ensure_running(ctx)
    on = a.get("on", True)
    await ctx.osa(f'tell application "Spotify" to set shuffling to {"true" if on else "false"}')
    return "Shuffle on." if on else "Shuffle off."


async def _set_repeat(ctx: ToolContext, a: dict[str, Any]) -> str:
    await _ensure_running(ctx)
    on = a.get("on", True)
    await ctx.osa(f'tell application "Spotify" to set repeating to {"true" if on else "false"}')
    return "Repeat on." if on else "Repeat off."


def register(reg: ToolRegistry) -> None:
    reg.register(Tool(
        "play_my_playlist",
        "Play one of the USER'S OWN Spotify playlists by (part of) its name, including playlists inside folders. "
        "Use this whenever the user says 'my playlist', names a playlist, or mentions a folder.",
        obj({"name": S("Playlist name as the user said it, without the word playlist"),
             "shuffle": {"type": "boolean", "description": "Shuffle it"}}, ["name"]),
        _play_playlist, category="media", timeout_s=40))
    reg.register(Tool(
        "list_my_playlists", "List the user's Spotify playlists, optionally filtered by a word in the name.",
        obj({"filter": S("Optional word to filter by")}), _list_playlists, category="media", timeout_s=30))
    reg.register(Tool(
        "play_liked_songs", "Play the user's Spotify liked songs.", obj(), _play_liked, category="media"))
    reg.register(Tool(
        "queue_song", "Add a SONG, ARTIST or ALBUM to the end of the current Spotify queue, without interrupting "
        "what's playing. Spotify must already be playing something.",
        obj({"query": S("Song, artist or album")}, ["query"]), _queue_song, category="media", timeout_s=20))
    reg.register(Tool(
        "list_spotify_queue", "List what's coming up next in the Spotify queue.",
        obj(), _list_queue, category="media"))
    reg.register(Tool(
        "set_spotify_shuffle", "Turn Spotify shuffle on or off.",
        obj({"on": B("true to shuffle, false to turn off")}, ["on"]), _set_shuffle, category="media"))
    reg.register(Tool(
        "set_spotify_repeat", "Turn Spotify repeat on or off.",
        obj({"on": B("true to repeat, false to turn off")}, ["on"]), _set_repeat, category="media"))


__all__ = ["register", "auth_url", "exchange_code", "all_playlists", "best_playlists", "E"]
