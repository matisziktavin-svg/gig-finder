"""Last.fm API client.

Docs: https://www.last.fm/api/intro
Auth: requires LASTFM_API_KEY in environment.
User endpoints additionally need LASTFM_USERNAME.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

API_URL = "https://ws.audioscrobbler.com/2.0/"


def _api_key() -> str:
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        raise RuntimeError("LASTFM_API_KEY not set in environment.")
    return key


def _username() -> str:
    name = os.environ.get("LASTFM_USERNAME")
    if not name:
        raise RuntimeError("LASTFM_USERNAME not set in environment.")
    return name


def _call(client: httpx.Client, method: str, **params: Any) -> dict:
    p = {"method": method, "api_key": _api_key(), "format": "json", **params}
    r = client.get(API_URL, params=p, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Last.fm error {data['error']}: {data.get('message')}")
    return data


def get_top_artists(period: str = "overall", limit: int = 50, page: int = 1) -> list[dict]:
    """period: overall | 7day | 1month | 3month | 6month | 12month"""
    with httpx.Client() as c:
        data = _call(c, "user.getTopArtists",
                     user=_username(), period=period, limit=limit, page=page)
    artists = data.get("topartists", {}).get("artist", []) or []
    return [
        {
            "name": a["name"],
            "mbid": a.get("mbid") or None,
            "playcount": int(a.get("playcount", 0) or 0),
            "url": a.get("url"),
        }
        for a in artists
    ]


def get_top_tags(limit: int = 30) -> list[dict]:
    with httpx.Client() as c:
        data = _call(c, "user.getTopTags", user=_username(), limit=limit)
    tags = data.get("toptags", {}).get("tag", []) or []
    return [{"name": t["name"], "count": int(t.get("count", 0) or 0)} for t in tags]


def get_loved_tracks(limit: int = 200) -> list[dict]:
    with httpx.Client() as c:
        data = _call(c, "user.getLovedTracks", user=_username(), limit=limit)
    tracks = data.get("lovedtracks", {}).get("track", []) or []
    return [
        {
            "name": t["name"],
            "artist": t["artist"]["name"],
            "mbid": t.get("mbid") or None,
        }
        for t in tracks
    ]


def get_recent_tracks(limit: int = 50) -> list[dict]:
    with httpx.Client() as c:
        data = _call(c, "user.getRecentTracks", user=_username(), limit=limit)
    tracks = data.get("recenttracks", {}).get("track", []) or []
    return [
        {
            "name": t["name"],
            "artist": t["artist"].get("#text") or t["artist"].get("name"),
            "mbid": t.get("mbid") or None,
        }
        for t in tracks
    ]


def get_similar_artists(
    artist: str | None = None,
    mbid: str | None = None,
    limit: int = 30,
) -> list[dict]:
    """Return similar artists. Pass mbid when available for accuracy."""
    if not artist and not mbid:
        raise ValueError("Pass either artist or mbid.")
    params: dict[str, Any] = {"limit": limit, "autocorrect": 1}
    if mbid:
        params["mbid"] = mbid
    if artist:
        params["artist"] = artist
    with httpx.Client() as c:
        data = _call(c, "artist.getSimilar", **params)
    similars = data.get("similarartists", {}).get("artist", []) or []
    return [
        {
            "name": s["name"],
            "mbid": s.get("mbid") or None,
            "match": float(s.get("match", 0) or 0),
        }
        for s in similars
    ]


def get_artist_tags(
    artist: str | None = None,
    mbid: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Top crowdsourced tags (genres) for an artist. Replaces Spotify genres,
    which were removed in the Feb 2026 API changes."""
    if not artist and not mbid:
        raise ValueError("Pass either artist or mbid.")
    params: dict[str, Any] = {"autocorrect": 1}
    if mbid:
        params["mbid"] = mbid
    if artist:
        params["artist"] = artist
    with httpx.Client() as c:
        data = _call(c, "artist.getTopTags", **params)
    tags = data.get("toptags", {}).get("tag", []) or []
    return [{"name": t["name"], "count": int(t.get("count", 0) or 0)}
            for t in tags[:limit]]


# ── smoke test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lib.env import load_env
    load_env()

    print(f"User: {_username()}\n")
    print("Top 10 artists (overall):")
    for a in get_top_artists(period="overall", limit=10):
        print(f"  {a['playcount']:>6} plays — {a['name']}")
    print("\nTop 10 tags:")
    for t in get_top_tags(limit=10):
        print(f"  {t['count']:>4} — {t['name']}")
    print("\nSimilar to top artist:")
    top = get_top_artists(period="overall", limit=1)
    if top:
        for s in get_similar_artists(artist=top[0]["name"], limit=5):
            print(f"  {s['match']:.3f} — {s['name']}")
