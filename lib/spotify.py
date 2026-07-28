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


def _send(client: httpx.Client, method: str, path: str, json_body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_access_token()}"}
    r = client.request(method, f"{API}/{path}", json=json_body, headers=headers, timeout=30)
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


def _norm(name: str) -> str:
    """Normalize an artist name for matching. Mirrors ranking.normalize_artist_name."""
    from lib.ranking import normalize_artist_name
    return normalize_artist_name(name)


ARTIST_MATCH_THRESHOLD = 85  # rapidfuzz ratio floor for accepting a search hit


def _best_artist_match(items: list[dict], name: str,
                       threshold: int = ARTIST_MATCH_THRESHOLD) -> dict | None:
    """Pick the best artist match from search results, or None. Pure.

    An exact normalized-name match always wins; otherwise the highest fuzzy
    ratio is accepted only if it clears `threshold`. This stops a typo or a
    non-artist name from silently resolving to an unrelated artist.
    """
    from rapidfuzz import fuzz

    if not items:
        return None
    target = _norm(name)
    best = None
    best_score = -1.0
    for a in items:
        cand = _norm(a.get("name", ""))
        if cand == target:
            return {"id": a["id"], "name": a["name"]}
        score = fuzz.ratio(cand, target)
        if score > best_score:
            best_score, best = score, a
    if best is not None and best_score >= threshold:
        return {"id": best["id"], "name": best["name"]}
    return None


def search_artist(name: str) -> dict | None:
    """Resolve an artist name to a Spotify artist {id, name}, or None.

    Prefers an exact normalized-name match; otherwise accepts the closest
    fuzzy match only if it clears ARTIST_MATCH_THRESHOLD.
    """
    with httpx.Client() as c:
        data = _get(c, "search", q=name, type="artist", limit=5)
    items = ((data.get("artists") or {}).get("items")) or []
    return _best_artist_match(items, name)


def get_artist_top_tracks(artist_id: str, market: str = "from_token",
                          limit: int = 2) -> list[dict]:
    """An artist's canonical top tracks, each as {name, id, uri}. Capped at `limit`.

    NOTE: /artists/{id}/top-tracks is blocked (HTTP 403) for Spotify apps that
    are still in development mode (not granted extended quota). Callers that
    want a resilient result should use get_artist_sampler_tracks, which falls
    back to a track search when this endpoint is unavailable.
    """
    if artist_id.startswith("spotify:artist:"):
        artist_id = artist_id.split(":")[-1]
    with httpx.Client() as c:
        data = _get(c, f"artists/{artist_id}/top-tracks", market=market)
    tracks: list[dict] = []
    for t in data.get("tracks", []) or []:
        tid = t.get("id")
        if not tid:
            continue
        tracks.append({
            "name": t.get("name"),
            "id": tid,
            "uri": t.get("uri") or f"spotify:track:{tid}",
        })
        if len(tracks) >= limit:
            break
    return tracks


# Set once per process after the top-tracks endpoint returns 403, so we don't
# re-probe a blocked endpoint for every artist in a sampler run.
_top_tracks_blocked = False


def _pick_tracks_from_search(items: list[dict], artist_id: str,
                             artist_name: str, limit: int) -> list[dict]:
    """Choose up to `limit` tracks (as {name,id,uri}) from track-search results,
    preferring an exact artist-id match, then an artist-name match. Pure."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(t: dict) -> None:
        tid = t.get("id")
        if not tid or tid in seen:
            return
        seen.add(tid)
        out.append({"name": t.get("name"), "id": tid,
                    "uri": t.get("uri") or f"spotify:track:{tid}"})

    for t in items:  # pass 1: track credits this exact artist id
        if any(a.get("id") == artist_id for a in t.get("artists", [])):
            add(t)
            if len(out) >= limit:
                return out
    for t in items:  # pass 2: artist name matches (covers id gaps)
        if any(_norm(a.get("name", "")) == _norm(artist_name)
               for a in t.get("artists", [])):
            add(t)
            if len(out) >= limit:
                return out
    return out[:limit]


def _search_artist_tracks(artist_id: str, artist_name: str, limit: int) -> list[dict]:
    """Fallback for get_artist_sampler_tracks: search tracks by artist name."""
    with httpx.Client() as c:
        data = _get(c, "search", q=f'artist:"{artist_name}"',
                    type="track", limit=min(max(limit * 3, limit), 10))
    items = ((data.get("tracks") or {}).get("items")) or []
    return _pick_tracks_from_search(items, artist_id, artist_name, limit)


def get_artist_sampler_tracks(artist: dict, market: str = "from_token",
                              limit: int = 2) -> list[dict]:
    """Up to `limit` representative tracks for an artist {id, name}.

    Prefers canonical top tracks; if that endpoint is blocked (403 — dev-mode
    apps) or returns nothing, falls back to a popularity-ranked track search.
    A 401 (expired auth) is re-raised so callers can prompt for re-auth.
    """
    global _top_tracks_blocked
    if not _top_tracks_blocked:
        try:
            tracks = get_artist_top_tracks(artist["id"], market=market, limit=limit)
            if tracks:
                return tracks
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise
            if e.response.status_code == 403:
                _top_tracks_blocked = True  # app lacks extended quota; use search
            else:
                raise
    return _search_artist_tracks(artist["id"], artist["name"], limit)


def find_or_create_playlist(name: str, public: bool = False,
                            description: str = "") -> dict:
    """Return the user's playlist named exactly `name`, creating it if absent.

    Returns {id, name, created: bool}.
    """
    for p in get_user_playlists(owned_only=True):
        if p["name"] == name:
            return {"id": p["id"], "name": p["name"], "created": False}
    with httpx.Client() as c:
        created = _send(c, "POST", "me/playlists", {
            "name": name,
            "public": public,
            "description": description,
        })
    return {"id": created["id"], "name": created["name"], "created": True}


def replace_playlist_tracks(playlist_id: str, track_uris: list[str]) -> None:
    """Replace ALL tracks in a playlist with `track_uris` (clears, then fills).

    A PUT with the first (up to 100) URIs replaces the whole playlist in one
    call; any overflow is appended in 100-URI chunks. Passing [] clears it.
    """
    with httpx.Client() as c:
        head = track_uris[:100]
        _send(c, "PUT", f"playlists/{playlist_id}/items", {"uris": head})
        for i in range(100, len(track_uris), 100):
            _send(c, "POST", f"playlists/{playlist_id}/items",
                  {"uris": track_uris[i:i + 100]})


def get_known_artist_names() -> set[str]:
    """Normalized names of artists the user already knows: everyone they
    follow plus every artist on a liked/saved track. Used to skip artists
    the user has already listened to when building a sampler."""
    known: set[str] = set()
    for a in get_followed_artists():
        known.add(_norm(a["name"]))
    for song in get_liked_songs():
        for artist_name in song.get("artists", []):
            known.add(_norm(artist_name))
    return known


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
