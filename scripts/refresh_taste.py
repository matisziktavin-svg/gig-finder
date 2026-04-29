"""Build/refresh the taste profile at ~/.gigfinder/taste.json.

Aggregates listening data from Last.fm (primary signal — actual play counts)
and Spotify (complementary — likes, follows, playlist curation), assigns
weighted scores per artist, fetches Last.fm tags (genres) for top artists,
and writes a single canonical profile to be consumed by find-gigs.
"""
from __future__ import annotations

import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make `lib` importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import cache, env, lastfm, spotify

env.load_env()

# ── Tuning knobs ────────────────────────────────────────────────────────
TAG_TOP_N_ARTISTS = 100      # fetch Last.fm tags for top N artists
LOVED_MULTIPLIER = 1.5
FOLLOWED_MULTIPLIER = 1.3
PLAYLIST_ONLY_BASE = 5       # baseline score for artists with no Last.fm playcount


def _normalize(name: str) -> str:
    """Match key for cross-source artist names."""
    s = name.lower().strip()
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\s+", " ", s)
    return s


def _add_artist(profile: dict, name: str, source: str, **fields: Any) -> None:
    """Get-or-create artist entry; merge a source signal into it."""
    key = _normalize(name)
    a = profile.get(key)
    if not a:
        a = {
            "name": name,
            "name_normalized": key,
            "mbid": None,
            "playcount_lastfm": 0,
            "playlist_count": 0,
            "liked_count": 0,
            "sources": [],
        }
        profile[key] = a
    if source not in a["sources"]:
        a["sources"].append(source)
    for k, v in fields.items():
        if k == "mbid" and v and not a.get("mbid"):
            a["mbid"] = v
        elif k == "playcount_lastfm" and v:
            a["playcount_lastfm"] = max(a.get("playcount_lastfm", 0), v)
        elif k == "playlist_count":
            a["playlist_count"] = a.get("playlist_count", 0) + v
        elif k == "liked_count":
            a["liked_count"] = a.get("liked_count", 0) + v


def collect_lastfm(profile: dict) -> None:
    print("Last.fm: top artists (overall)...", flush=True)
    for a in lastfm.get_top_artists(period="overall", limit=200):
        _add_artist(profile, a["name"], source="lastfm_top_overall",
                    mbid=a.get("mbid"), playcount_lastfm=a["playcount"])

    print("Last.fm: top artists (12 months)...", flush=True)
    for a in lastfm.get_top_artists(period="12month", limit=100):
        _add_artist(profile, a["name"], source="lastfm_top_12m",
                    mbid=a.get("mbid"), playcount_lastfm=a["playcount"])

    print("Last.fm: top artists (1 month)...", flush=True)
    for a in lastfm.get_top_artists(period="1month", limit=50):
        _add_artist(profile, a["name"], source="lastfm_top_1m",
                    mbid=a.get("mbid"))

    print("Last.fm: loved tracks...", flush=True)
    for t in lastfm.get_loved_tracks(limit=200):
        _add_artist(profile, t["artist"], source="lastfm_loved")

    print("Last.fm: recent tracks...", flush=True)
    for t in lastfm.get_recent_tracks(limit=50):
        _add_artist(profile, t["artist"], source="lastfm_recent")


def collect_spotify(profile: dict) -> None:
    print("Spotify: followed artists...", flush=True)
    for a in spotify.get_followed_artists():
        _add_artist(profile, a["name"], source="spotify_followed")

    print("Spotify: liked songs (this can take a minute)...", flush=True)
    for song in spotify.get_liked_songs():
        for artist_name in song["artists"]:
            _add_artist(profile, artist_name, source="spotify_liked", liked_count=1)

    playlists = spotify.get_user_playlists(owned_only=True)
    print(f"Spotify: mining {len(playlists)} owned playlists...", flush=True)
    for i, p in enumerate(playlists, 1):
        if p["total_tracks"] == 0:
            continue
        if i % 10 == 0:
            print(f"  ({i}/{len(playlists)})", flush=True)
        try:
            for a in spotify.get_playlist_artists(p["id"]):
                _add_artist(profile, a["name"], source="spotify_playlist",
                            playlist_count=1)
        except Exception as e:
            print(f"  WARN: skipping {p['name']}: {e}", flush=True)


def compute_score(a: dict) -> float:
    pc = a.get("playcount_lastfm", 0)
    if pc > 0:
        base = math.log(pc + 1) * 10
    else:
        base = (a.get("playlist_count", 0) * 2
                + a.get("liked_count", 0) * 3
                + PLAYLIST_ONLY_BASE)
    if "lastfm_loved" in a["sources"]:
        base *= LOVED_MULTIPLIER
    if "spotify_followed" in a["sources"]:
        base *= FOLLOWED_MULTIPLIER
    return round(base, 3)


def fetch_tags(artists: list[dict]) -> None:
    """Mutate top artists in-place with 'tags' from Last.fm artist.getTopTags."""
    print(f"Last.fm: fetching tags for top {len(artists)} artists...", flush=True)
    for i, a in enumerate(artists, 1):
        if i % 20 == 0:
            print(f"  ({i}/{len(artists)})", flush=True)
        try:
            tags = lastfm.get_artist_tags(
                artist=a["name"], mbid=a.get("mbid"), limit=5,
            )
            a["tags"] = [t["name"] for t in tags]
        except Exception as e:
            print(f"  WARN: tags for {a['name']}: {e}", flush=True)
            a["tags"] = []


def main() -> int:
    started = time.time()
    profile_dict: dict = {}

    collect_lastfm(profile_dict)
    collect_spotify(profile_dict)

    artists = list(profile_dict.values())
    for a in artists:
        a["score"] = compute_score(a)
    artists.sort(key=lambda x: x["score"], reverse=True)

    fetch_tags(artists[:TAG_TOP_N_ARTISTS])

    # Aggregate genre weights across tagged artists
    genre_weights: dict[str, float] = {}
    for a in artists[:TAG_TOP_N_ARTISTS]:
        for tag in a.get("tags", []):
            genre_weights[tag] = genre_weights.get(tag, 0) + a["score"]
    genres = sorted(
        [{"name": g, "weight": round(w, 2)} for g, w in genre_weights.items()],
        key=lambda x: x["weight"], reverse=True,
    )

    profile = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "total_artists": len(artists),
            "tagged_artists": min(TAG_TOP_N_ARTISTS, len(artists)),
            "total_genres": len(genres),
            "elapsed_seconds": round(time.time() - started, 1),
        },
        "artists": artists,
        "genres": genres,
    }
    cache.save_json(cache.TASTE_PATH, profile)

    print(f"\nWrote {cache.TASTE_PATH}")
    print(f"  {len(artists)} artists | {len(genres)} genres | "
          f"{profile['stats']['elapsed_seconds']}s")
    print(f"\nTop 10 by score:")
    for a in artists[:10]:
        tags = a.get("tags", [])[:3]
        print(f"  {a['score']:>7.1f}  {a['name']:<32}  tags={tags}")
    print(f"\nTop 10 genres:")
    for g in genres[:10]:
        print(f"  {g['weight']:>7.1f}  {g['name']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
