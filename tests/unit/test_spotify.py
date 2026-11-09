from __future__ import annotations

import pytest
import respx
from httpx import Response

from jarvis_core.skills import spotify

PLAYLISTS = [
    {"name": "Late Night Study", "uri": "spotify:playlist:aaa"},
    {"name": "Gym Hype", "uri": "spotify:playlist:bbb"},
    {"name": "Study Beats (Focus)", "uri": "spotify:playlist:ccc"},
    {"name": "Road Trip 2024", "uri": "spotify:playlist:ddd"},
]


def test_fuzzy_playlist_match_ignores_filler_words():
    hits = spotify.best_playlists("my gym hype playlist", PLAYLISTS)
    assert hits[0][1]["uri"] == "spotify:playlist:bbb"
    assert spotify.best_playlists("late night studying", PLAYLISTS)[0][1]["name"] == "Late Night Study"
    assert spotify.best_playlists("zzzz nothing like it", PLAYLISTS) == []


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    spotify._cache.update(token=None, exp=0.0, playlists=None, playlists_at=0.0)
    store = {"SPOTIFY_CLIENT_ID": "cid", "SPOTIFY_CLIENT_SECRET": "sec", "SPOTIFY_REFRESH_TOKEN": "rt"}
    monkeypatch.setattr(spotify.secrets, "get_secret", lambda k, **kw: store.get(k))
    monkeypatch.setattr(spotify.secrets, "set_secret", lambda k, v: store.__setitem__(k, v))


@respx.mock
async def test_lists_all_pages_and_plays_playlist(monkeypatch):
    respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 3600}))
    respx.get("https://api.spotify.com/v1/me/playlists?limit=50").mock(
        return_value=Response(200, json={"items": PLAYLISTS[:2], "next": "https://api.spotify.com/v1/me/playlists?offset=2"}))
    respx.get("https://api.spotify.com/v1/me/playlists?offset=2").mock(
        return_value=Response(200, json={"items": PLAYLISTS[2:], "next": None}))
    played = []

    class Ctx:
        async def osa(self, script, *args, **kw):
            played.append(args)
            return ""

    async def noop(ctx):
        return None

    monkeypatch.setattr(spotify, "_ensure_running", noop)
    out = await spotify._play_playlist(Ctx(), {"name": "Road Trip"})
    assert "Road Trip 2024" in out
    assert played == [("spotify:playlist:ddd",)]
    with pytest.raises(spotify.SkillError):
        await spotify._play_playlist(Ctx(), {"name": "qqqqq xylophone"})


async def test_not_signed_in_gives_clear_message(monkeypatch):
    monkeypatch.setattr(spotify.secrets, "get_secret", lambda k, **kw: "x" if k != "SPOTIFY_REFRESH_TOKEN" else None)
    with pytest.raises(spotify.SkillError, match="spotify-login"):
        await spotify.user_token()


class _Ctx:
    async def osa(self, script, *args, **kw):
        return ""


@respx.mock
async def test_queue_song_finds_and_queues_it(monkeypatch):
    import jarvis_core.skills.media as media

    respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 3600}))
    queued = []

    async def fake_find(token, q):
        return "spotify:track:zzz", "Clair de Lune by Debussy"

    async def noop(ctx):
        return None

    monkeypatch.setattr(spotify, "_ensure_running", noop)
    monkeypatch.setattr(media, "_spotify_find", fake_find)

    def _record(request):
        queued.append(request.url.params.get("uri"))
        return Response(200)

    respx.post("https://api.spotify.com/v1/me/player/queue").mock(side_effect=_record)
    out = await spotify._queue_song(_Ctx(), {"query": "clair de lune"})
    assert out == "Queued Clair de Lune by Debussy."
    assert queued == ["spotify:track:zzz"]


@respx.mock
async def test_queue_song_reports_no_active_device(monkeypatch):
    import jarvis_core.skills.media as media

    respx.post("https://accounts.spotify.com/api/token").mock(
        return_value=Response(200, json={"access_token": "tok", "expires_in": 3600}))
    respx.post("https://api.spotify.com/v1/me/player/queue").mock(return_value=Response(404))

    async def fake_find(token, q):
        return "spotify:track:zzz", "Some Song"

    async def noop(ctx):
        return None

    monkeypatch.setattr(spotify, "_ensure_running", noop)
    monkeypatch.setattr(media, "_spotify_find", fake_find)
    with pytest.raises(spotify.SkillError, match="playing"):
        await spotify._queue_song(_Ctx(), {"query": "some song"})
