"""MusicBrainz API client (fallback for similar-artist lookups when Last.fm has no data).

Docs: https://musicbrainz.org/doc/MusicBrainz_API
No auth required, but the rate limit is strict: 1 request/second per User-Agent.
We send a polite User-Agent string identifying the gig-finder skill.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

API = "https://musicbrainz.org/ws/2"
USER_AGENT = "gig-finder/0.1 (https://github.com/matisziktavin-svg/gig-finder)"

_last_call_at: float = 0.0


def _throttle() -> None:
    """MusicBrainz allows ~1 req/sec — sleep if we'd exceed that."""
    global _last_call_at
    now = time.time()
    elapsed = now - _last_call_at
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)
    _last_call_at = time.time()


def _get(client: httpx.Client, path: str, **params: Any) -> dict:
    _throttle()
    p = {"fmt": "json", **params}
    r = client.get(f"{API}/{path}", params=p, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    return r.json()


def search_artist(name: str) -> dict | None:
    """Find an artist by name; return the top match (with mbid) or None."""
    with httpx.Client() as c:
        data = _get(c, "artist", query=name, limit=1)
    artists = data.get("artists", []) or []
    if not artists:
        return None
    a = artists[0]
    return {
        "name": a.get("name"),
        "mbid": a.get("id"),
        "score": int(a.get("score", 0) or 0),
    }


def get_related_artists(mbid: str) -> list[dict]:
    """Return artists linked via 'related' relationships in MusicBrainz."""
    with httpx.Client() as c:
        data = _get(c, f"artist/{mbid}", inc="artist-rels")
    out: list[dict] = []
    for rel in data.get("relations", []) or []:
        target = rel.get("artist") or {}
        if not target.get("id"):
            continue
        out.append({
            "name": target.get("name"),
            "mbid": target.get("id"),
            "type": rel.get("type"),
        })
    return out


# ── smoke test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    name = sys.argv[1] if len(sys.argv) > 1 else "Radiohead"
    print(f"Searching MusicBrainz for: {name}")
    a = search_artist(name)
    if not a:
        print("  no match")
        sys.exit(1)
    print(f"  -> {a['name']}  mbid={a['mbid']}  score={a['score']}")
    related = get_related_artists(a["mbid"])
    print(f"\nRelated artists ({len(related)}):")
    for r in related[:15]:
        print(f"  [{r['type']}] {r['name']}  ({r['mbid']})")
