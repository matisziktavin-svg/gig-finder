"""Read Claude-extracted events from stdin, filter against the taste profile
+ Last.fm similar expansion, tag, sort, and emit JSON.

Usage:
  cat events.json | python scripts/finalize_search.py \
        --from 2026-06-01 --to 2026-06-15

Input shape (events on stdin):
  [
    {
      "date": "YYYY-MM-DD",          # required
      "time": "HH:MM",               # optional
      "venue": "Venue name",         # required
      "venue_url": "https://...",    # optional
      "artists": ["Artist 1", ...],  # required
      "ticket_url": "https://...",   # optional
    }, ...
  ]

Output (stdout):
  {
    "ok": true,
    "stats": {input_events, matched_events, tier1_count, tier2_count},
    "events": [{...with primary_tag, secondary_tags, matched_artists...}]
  }
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import cache, env, ranking

env.load_env()


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="date_from", required=True)
    p.add_argument("--to", dest="date_to", required=True)
    args = p.parse_args()
    df = _parse_date(args.date_from)
    dt = _parse_date(args.date_to)

    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"ok": False, "error": "No events on stdin"}))
        return 1
    try:
        input_events = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"Invalid JSON: {e}"}))
        return 1
    if not isinstance(input_events, list):
        print(json.dumps({"ok": False, "error": "Expected a JSON array"}))
        return 1

    taste = cache.load_json(cache.TASTE_PATH)
    if not taste:
        print(json.dumps({
            "ok": False,
            "error": "No taste profile. Run scripts/refresh_taste.py first.",
        }))
        return 1

    universe = ranking.build_artist_universe(taste)

    matched: list[dict] = []
    tier1_count = 0
    tier2_count = 0
    for ev in input_events:
        # Date filter
        try:
            d = _parse_date(ev.get("date", ""))
        except ValueError:
            continue
        if not (df <= d <= dt):
            continue

        matches = ranking.match_event(ev, universe)
        if not matches:
            continue
        primary, secondary = ranking.tag_event(matches, taste)
        out = {
            "date": ev["date"],
            "time": ev.get("time"),
            "venue": ev.get("venue"),
            "venue_url": ev.get("venue_url"),
            "artists": ev.get("artists", []),
            "matched_artists": [m["name"] for m in matches],
            "primary_tag": primary,
            "secondary_tags": secondary,
            "ticket_url": ev.get("ticket_url"),
        }
        matched.append(out)
        if any(m["tier"] == 1 for m in matches):
            tier1_count += 1
        else:
            tier2_count += 1

    matched = ranking.sort_events_by_date(matched)

    print(json.dumps({
        "ok": True,
        "stats": {
            "input_events": len(input_events),
            "matched_events": len(matched),
            "tier1_count": tier1_count,
            "tier2_count": tier2_count,
            "universe_size": len(universe),
        },
        "events": matched,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
