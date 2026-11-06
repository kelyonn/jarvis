"""Media playback and audio volume."""

from __future__ import annotations

import re
from typing import Any

from jarvis_core.errors import SkillError
from jarvis_core.skills.apps import running_apps
from jarvis_core.skills.base import B, E, I, S, Tool, ToolContext, ToolRegistry, obj

_VERBS = {
    "play": "play", "pause": "pause", "toggle": "playpause", "next": "next track",
    "previous": "previous track",
}


async def _player(ctx: ToolContext, preferred: str | None = None) -> str:
    if preferred:
        p = preferred.strip().lower()
        if p in {"spotify", "music", "apple music"}:
            return "Spotify" if p == "spotify" else "Music"
    running = await running_apps(ctx)
    for cand in ("Spotify", "Music"):
        if cand in running:
            return cand
    return "Spotify" if (ctx.app_dirs and any((d / "Spotify.app").exists() for d in ctx.app_dirs)) else "Music"


# AppleScript only knows a player's own words ("next track", "player state") when the app name is a literal at compile
# time, so these scripts are built per app from a fixed whitelist (never from user text).
_APPS = {"Spotify", "Music"}


async def _control(ctx: ToolContext, a: dict[str, Any]) -> str:
    verb = _VERBS[a["action"]]
    app = await _player(ctx, a.get("app"))
    assert app in _APPS
    if app not in await running_apps(ctx):
        if a["action"] in ("pause", "next", "previous"):
            return f"{app} isn't running, sir."
        await ctx.run(["open", "-a", app])
        import asyncio

        await asyncio.sleep(2.5)
    await ctx.osa(f'tell application "{app}" to {verb}')
    return f"{a['action'].capitalize()} on {app}."


async def _now_playing(ctx: ToolContext, a: dict[str, Any]) -> str:
    app = await _player(ctx, a.get("app"))
    assert app in _APPS
    if app not in await running_apps(ctx):
        return "Nothing is playing."
    out = await ctx.osa(
        f'tell application "{app}"\nif player state is stopped then return ""\n'
        'return (name of current track) & " by " & (artist of current track)\nend tell'
    )
    return out or "Nothing is playing."


_SPOTIFY_HELP = "Run jarvisd spotify-setup once and I can play songs directly."


async def _spotify_token() -> str | None:
    """App-only token (client-credentials flow) for catalogue search. Needs a free developer app, see `spotify-setup`."""
    import httpx

    from jarvis_core import secrets

    cid, sec = secrets.get_secret("SPOTIFY_CLIENT_ID"), secrets.get_secret("SPOTIFY_CLIENT_SECRET")
    if not (cid and sec):
        return None
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                         auth=(cid, sec))
    if r.status_code != 200:
        raise SkillError("Spotify rejected my credentials, sir. Run jarvisd spotify-setup again.")
    return r.json().get("access_token")


def _split_query(q: str) -> tuple[str, str | None]:
    m = re.match(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", q, re.I)
    return (m["title"].strip(), m["artist"].strip()) if m else (q, None)


async def _spotify_find(token: str, q: str) -> tuple[str, str] | None:
    """Return (uri, spoken description) for the best match."""
    import httpx

    title, artist = _split_query(q)
    queries = [f"track:{title} artist:{artist}", f"{title} {artist}"] if artist else []
    queries.append(q)
    hdr = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=8, headers=hdr) as c:
        for query in queries:
            r = await c.get("https://api.spotify.com/v1/search",
                            params={"q": query, "type": "track,artist", "limit": 3})
            if r.status_code != 200:
                continue
            data = r.json()
            norm = re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()
            for art in data.get("artists", {}).get("items", []):  # "play the killers" -> the artist, not one song
                if not artist and re.sub(r"[^a-z0-9 ]", "", art["name"].lower()).strip() == norm:
                    return art["uri"], art["name"]
            tracks = data.get("tracks", {}).get("items", [])
            if tracks:
                t = tracks[0]
                return t["uri"], f"{t['name']} by {t['artists'][0]['name']}"
    return None


async def _ensure_spotify(ctx: ToolContext) -> None:
    import asyncio

    if "Spotify" in await running_apps(ctx):
        return
    res = await ctx.run(["open", "-a", "Spotify"])
    if not res.ok:
        raise SkillError("I couldn't open Spotify, sir. Is it installed?")
    for _ in range(30):
        await asyncio.sleep(0.5)
        if "Spotify" in await running_apps(ctx):
            await asyncio.sleep(1.5)  # let it finish launching before the first command
            return
    raise SkillError("Spotify is taking too long to start, sir.")


async def _play_music(ctx: ToolContext, a: dict[str, Any]) -> str:
    q = a["query"].strip()
    app = await _player(ctx, a.get("app"))
    if app == "Music":
        out = await ctx.osa(
            'on run argv\nset q to item 1 of argv\ntell application "Music"\n'
            "set hits to (every track of library playlist 1 whose name contains q or artist contains q or album contains q)\n"
            'if (count of hits) is 0 then return ""\nplay (item 1 of hits)\n'
            'return (name of item 1 of hits) & " by " & (artist of item 1 of hits)\nend tell\nend run',
            q,
        )
        if not out:
            raise SkillError(f"I couldn't find {q} in your library, sir.")
        return f"Playing {out}."
    await _ensure_spotify(ctx)
    token = await _spotify_token()
    if token is None:
        await ctx.run(["open", f"spotify:search:{q}"])
        return f"I've opened Spotify's search for {q}, but I can't press play myself yet. {_SPOTIFY_HELP}"
    found = await _spotify_find(token, q)
    if not found:
        raise SkillError(f"I couldn't find {q} on Spotify, sir.")
    uri, spoken = found
    await ctx.osa('on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run', uri)
    return f"Playing {spoken}."


# -- volume ---------------------------------------------------------------------


async def get_volume(ctx: ToolContext) -> tuple[int, bool]:
    out = await ctx.osa("output volume of (get volume settings) & \",\" & output muted of (get volume settings)")
    vol, _, muted = out.partition(",")
    try:
        return int(float(vol)), muted.strip().lower() == "true"
    except ValueError:
        return 0, False


async def _set_volume(ctx: ToolContext, a: dict[str, Any]) -> str:
    level = max(0, min(100, int(a["level"])))
    await ctx.osa(f"set volume output volume {level}")
    await ctx.osa("set volume output muted false")
    return f"Volume at {level} percent."


async def _change_volume(ctx: ToolContext, a: dict[str, Any]) -> str:
    cur, _ = await get_volume(ctx)
    step = int(a.get("amount") or 10)
    new = max(0, min(100, cur + (step if a["direction"] == "up" else -step)))
    await ctx.osa(f"set volume output volume {new}")
    return f"Volume at {new} percent."


async def _mute(ctx: ToolContext, a: dict[str, Any]) -> str:
    on = a.get("on", True)
    await ctx.osa(f"set volume output muted {'true' if on else 'false'}")
    return "Muted." if on else "Unmuted."


async def _get_volume(ctx: ToolContext, a: dict[str, Any]) -> str:
    v, m = await get_volume(ctx)
    return f"Volume {v} percent{', muted' if m else ''}."


def parse_volume_phrase(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*(%|percent)?", text)
    return int(m.group(1)) if m else None


def register(reg: ToolRegistry) -> None:
    app = {"app": S("Optional: 'spotify' or 'music'")}
    reg.register(Tool(
        "media_control", "Play, pause, toggle, skip or go back in Spotify or Apple Music.",
        obj({"action": E("What to do", list(_VERBS)), **app}, ["action"]), _control, category="media"))
    reg.register(Tool("now_playing", "What song is playing.", obj(app), _now_playing, category="media"))
    reg.register(Tool(
        "play_music", "Search for and play a SONG, ARTIST or ALBUM by name (Spotify catalogue or Apple Music library). "
        "Not for the user's own playlists or liked songs: use play_my_playlist / play_liked_songs for those.",
        obj({"query": S("Song, artist or album"), **app}, ["query"]), _play_music, category="media"))
    reg.register(Tool("set_volume", "Set output volume 0-100.", obj({"level": I("0-100")}, ["level"]), _set_volume, category="media"))
    reg.register(Tool(
        "change_volume", "Raise or lower volume.",
        obj({"direction": E("up or down", ["up", "down"]), "amount": I("Percent, default 10")}, ["direction"]),
        _change_volume, category="media"))
    reg.register(Tool("mute_volume", "Mute or unmute output.", obj({"on": B("true to mute")}), _mute, category="media"))
    reg.register(Tool("get_volume", "Current volume.", obj(), _get_volume, category="media"))
