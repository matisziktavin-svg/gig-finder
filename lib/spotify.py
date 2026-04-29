"""Spotify Web API client.

Reads (and refreshes) the same token cache file that spotify-mcp writes,
at SPOTIFY_CACHE_PATH (default ~/.spotify_mcp_cache.json). Uses the
SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET from environment to refresh.

Run `spotify-mcp --auth` once before using this module.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

API = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

CACHE_PATH = Path(os.environ.get(
    "SPOTIFY_CACHE_PATH",
    str(Path.home() / ".spotify_mcp_cache.json"),
))


def _client_creds() -> tuple[str, str]:
    cid = os.environ.get("SPOTIFY_CLIENT_ID")
    sec = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not sec:
        raise RuntimeError("SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set.")
    return cid, sec


def _load_token() -> dict:
    if not CACHE_PATH.exists():
        raise RuntimeError(
            f"No Spotify token cache at {CACHE_PATH}. "
            "Run `spotify-mcp --auth` first."
        )
    with CACHE_PATH.open() as f:
        return json.load(f)


def _save_token(data: dict) -> None:
    with CACHE_PATH.open("w") as f:
        json.dump(data, f, indent=2)


def _refresh(refresh_token: str) -> dict:
    cid, sec = _client_creds()
    with httpx.Client() as c:
        r = c.post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cid,
            "client_secret": sec,
        }, timeout=30)
        r.raise_for_status()
        d = r.json()
    d["refresh_token"] = d.get("refresh_token", refresh_token)
    d["expires_at"] = time.time() + d.get("expires_in", 3600)
    _save_token(d)
    return d


def _access_token() -> str:
    t = _load_token()
    if t.get("expires_at", 0) < time.time() + 60:
        t = _refresh(t["refresh_token"])
    return t["access_token"]


def _get(client: httpx.Client, path: str, **params: Any) -> dict:
    headers = {"Authorization": f"Bearer {_access_token()}"}
    r = client.get(f"{API}/{path}", params=params, headers=headers, timeout=30)
    r.raise_for_status()
    if r.status_code == 204 or not r.content:
        return {}
    return r.json()


# ── Public functions ─────────────────────────────────────────────────────

def get_followed_artists() -> list[dict]:
    """Paginate /me/following?type=artist via cursor.

    Note: as of Feb 2026, Spotify no longer includes `genres` in this
    endpoint's artist objects. Use get_artist_genres([ids]) separately
    if you need genres.
    """
    artists: list[dict] = []
    after: str | None = None
    with httpx.Client() as c:
        while True:
            params: dict[str, Any] = {"type": "artist", "limit": 50}
            if after:
                params["after"] = after
            data = _get(c, "me/following", **params)
            block = data.get("artists") or {}
            items = block.get("items", []) or []
            for a in items:
                artists.append({"name": a["name"], "id": a["id"]})
            after = (block.get("cursors") or {}).get("after")
            if not items or not after:
                break
    return artists


def get_liked_songs(limit: int | None = None) -> list[dict]:
    """All saved tracks (or up to `limit`)."""
    songs: list[dict] = []
    offset = 0
    with httpx.Client() as c:
        while True:
            data = _get(c, "me/tracks", limit=50, offset=offset)
            items = data.get("items", []) or []
            if not items:
                break
            for item in items:
                t = item.get("track")
                if not t:
                    continue
                songs.append({
                    "name": t["name"],
                    "id": t["id"],
                    "artists": [a["name"] for a in t.get("artists", [])],
                    "artist_ids": [a["id"] for a in t.get("artists", []) if a.get("id")],
                })
                if limit and len(songs) >= limit:
                    return songs
            if not data.get("next"):
                break
            offset += 50
    return songs


def get_user_playlists(owned_only: bool = True) -> list[dict]:
    """User's playlists. owned_only filters to playlists the user created."""
    playlists: list[dict] = []
    offset = 0
    me_id: str | None = None
    with httpx.Client() as c:
        if owned_only:
            me_id = (_get(c, "me") or {}).get("id")
        while True:
            data = _get(c, "me/playlists", limit=50, offset=offset)
            items = data.get("items", []) or []
            if not items:
                break
            for p in items:
                if owned_only and p.get("owner", {}).get("id") != me_id:
                    continue
                # Feb 2026: playlist `tracks` field renamed to `items`
                count_block = p.get("items") or p.get("tracks") or {}
                playlists.append({
                    "id": p["id"],
                    "name": p["name"],
                    "total_tracks": count_block.get("total", 0),
                })
            if not data.get("next"):
                break
            offset += 50
    return playlists


def get_playlist_artists(playlist_id: str) -> list[dict]:
    """Unique artists from a playlist's tracks. Uses Feb 2026 /items endpoint."""
    artists: dict[str, dict] = {}
    offset = 0
    with httpx.Client() as c:
        while True:
            data = _get(c, f"playlists/{playlist_id}/items", limit=50, offset=offset)
            items = data.get("items", []) or []
            if not items:
                break
            for item in items:
                t = item.get("track") or item.get("item")
                if not t or t.get("type") != "track":
                    continue
                for a in t.get("artists", []):
                    aid = a.get("id")
                    if aid and aid not in artists:
                        artists[aid] = {"id": aid, "name": a["name"]}
            if not data.get("next"):
                break
            offset += 50
    return list(artists.values())


# ── smoke test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lib.env import load_env
    load_env()

    print(f"Token cache: {CACHE_PATH}")
    followed = get_followed_artists()
    print(f"\nFollowed artists: {len(followed)}")
    for a in followed[:10]:
        print(f"  - {a['name']}")

    playlists = get_user_playlists(owned_only=True)
    print(f"\nOwned playlists: {len(playlists)}")
    for p in playlists[:5]:
        print(f"  - {p['name']} ({p['total_tracks']} tracks)")
