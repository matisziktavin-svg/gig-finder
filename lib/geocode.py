"""Nominatim (OpenStreetMap) geocoder with on-disk cache.

Free, no key. Rate limit: 1 req/sec, must send a meaningful User-Agent.
Cache at ~/.gigfinder/geocode.cache.json.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from lib.cache import GEOCODE_CACHE_PATH, load_json, save_json

API = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "gig-finder/0.1 (https://github.com/matisziktavin-svg/gig-finder)"

_last_call_at: float = 0.0


def _throttle() -> None:
    global _last_call_at
    elapsed = time.time() - _last_call_at
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)
    _last_call_at = time.time()


def geocode(location: str) -> tuple[float, float] | None:
    """Return (lat, lng) for a freeform location string. Cached on disk."""
    cache: dict = load_json(GEOCODE_CACHE_PATH) or {}
    key = location.strip().lower()
    if key in cache:
        c = cache[key]
        return (c["lat"], c["lng"])

    _throttle()
    with httpx.Client() as client:
        r = client.get(
            API,
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    if not data:
        return None
    lat = float(data[0]["lat"])
    lng = float(data[0]["lon"])
    cache[key] = {
        "lat": lat,
        "lng": lng,
        "display_name": data[0].get("display_name"),
    }
    save_json(GEOCODE_CACHE_PATH, cache)
    return (lat, lng)


# ── smoke test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    loc = " ".join(sys.argv[1:]) or "Boston, MA"
    print(f"Geocoding: {loc}")
    result = geocode(loc)
    if result:
        print(f"  -> lat={result[0]}, lng={result[1]}")
        print(f"  cache: {GEOCODE_CACHE_PATH}")
    else:
        print("  no result")
