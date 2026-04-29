"""Validate find-gigs input, geocode the location, and emit JSON of the
paths/IDs Claude needs to drive the rest of the flow.

Usage:
  python scripts/prepare_search.py --location "Boston" --from 2026-06-01 --to 2026-06-15

Output (stdout): JSON object with keys:
  ok, location, city_key, lat, lng, date_from, date_to,
  taste_path, taste_age_days,
  venue_cache_path, venue_cache_age_days, venue_cache_stale,
  calendar_cache_dir
On error: { ok: false, error: "..." }
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import cache, env, geocode
from lib.city_key import city_key as to_city_key

env.load_env()

VENUES_TTL_DAYS = 365
TASTE_FRESH_DAYS = 30


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def _emit(obj: dict) -> int:
    print(json.dumps(obj, indent=2))
    return 0 if obj.get("ok") else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--location", required=True)
    p.add_argument("--from", dest="date_from", required=True)
    p.add_argument("--to", dest="date_to", required=True)
    args = p.parse_args()

    # Validate dates
    try:
        df = _parse_date(args.date_from)
        dt = _parse_date(args.date_to)
    except ValueError as e:
        return _emit({"ok": False, "error": f"Bad date: {e}"})
    if df > dt:
        return _emit({"ok": False, "error": "--from must be <= --to"})

    # Validate taste profile exists
    taste = cache.load_json(cache.TASTE_PATH)
    if not taste:
        return _emit({
            "ok": False,
            "error": (
                "No taste profile found. Run "
                "'python scripts/refresh_taste.py' first."
            ),
        })
    try:
        taste_updated_ts = (
            cache.TASTE_PATH.stat().st_mtime
        )
        taste_age_days = round((time.time() - taste_updated_ts) / 86400, 1)
    except OSError:
        taste_age_days = None

    # Geocode
    try:
        coords = geocode.geocode(args.location)
    except Exception as e:
        return _emit({"ok": False, "error": f"Geocoding failed: {e}"})
    if not coords:
        return _emit({"ok": False,
                      "error": f"No geocoding result for: {args.location!r}"})
    lat, lng = coords

    ck = to_city_key(args.location)

    # Venue cache state
    venue_cache_path = cache.CACHE_DIR / "venues" / f"{ck}.json"
    venue_cache_age = None
    venue_stale = True
    if venue_cache_path.exists():
        venue_cache_age = round(
            (time.time() - venue_cache_path.stat().st_mtime) / 86400, 1,
        )
        venue_stale = venue_cache_age > VENUES_TTL_DAYS
    cache.ensure_cache_dir()
    venue_cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Calendar cache dir
    calendar_dir = cache.CACHE_DIR / "calendars"
    calendar_dir.mkdir(parents=True, exist_ok=True)

    return _emit({
        "ok": True,
        "location": args.location,
        "city_key": ck,
        "lat": lat,
        "lng": lng,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "taste_path": str(cache.TASTE_PATH),
        "taste_age_days": taste_age_days,
        "taste_stale": (taste_age_days or 0) > TASTE_FRESH_DAYS,
        "venue_cache_path": str(venue_cache_path),
        "venue_cache_age_days": venue_cache_age,
        "venue_cache_stale": venue_stale,
        "calendar_cache_dir": str(calendar_dir),
        "venues_ttl_days": VENUES_TTL_DAYS,
    })


if __name__ == "__main__":
    sys.exit(main())
