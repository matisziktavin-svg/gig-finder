"""Deterministically scrape every stale venue calendar for a city, no LLM.

Pipeline position (see SKILL.md):

    prepare_search.py  ->  scrape_calendars.py  ->  finalize_search.py

What it does:
  1. Reads the city's venue list (~/.gigfinder/venues/<city_key>.json).
  2. For each venue whose calendar cache is stale (or --force), fetches the
     events page concurrently and extracts events (JSON-LD, ICS, Tribe REST,
     known WordPress/Webflow/ticketing markup -- see lib/venue_scrape.py).
  3. Writes each cache file in the standard format/path
     (~/.gigfinder/calendars/<sha256(events_url)[:16]>.json).
  4. Aggregates every venue's in-window events (fresh cache or just scraped,
     including ones an LLM filled in by hand) into one JSON array, ready for
     ``finalize_search.py --events-file``.
  5. Prints a JSON summary: per venue ok / partial / failed / cached, event
     count and reason, plus ``needs_llm`` -- the only venues the model should
     touch.

Usage:
  python scripts/scrape_calendars.py --location "St. Louis, MO" \
      --from 2026-10-12 --to 2026-11-12 [--force] [--only "city winery"]

Idempotent: re-run it after hand-filling a failed venue's cache file to
rebuild the aggregate events file.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import cache, env
from lib.city_key import city_key as to_city_key
from lib.venue_scrape import Fetcher, scrape_venue

env.load_env()

CALENDAR_TTL_DAYS = 1.0
DEFAULT_EVENTS_FILE = cache.CACHE_DIR / "gigfinder_events.json"


def _calendar_url(venue: dict) -> str | None:
    return venue.get("events_url") or venue.get("url")


def _status(path: Path, ttl_days: float) -> dict:
    """Same shape as cache_io.py's status output."""
    if not path.exists():
        return {"exists": False, "age_days": None, "stale": True}
    age = (time.time() - path.stat().st_mtime) / 86400
    return {"exists": True, "age_days": round(age, 2), "stale": age > ttl_days}


def _cache_covers(data: dict | None, win_to: date) -> bool:
    """A cache written by this script records the window it covered; a
    narrower cache must not satisfy a wider request. Hand-written caches have
    no window and are trusted (legacy behavior)."""
    if not data or "window_to" not in data:
        return bool(data)
    return data["window_to"] >= win_to.isoformat()


def _load(path: Path) -> dict | None:
    try:
        return cache.load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def _in_window(events: list[dict], df: date, dt: date) -> list[dict]:
    lo, hi = df.isoformat(), dt.isoformat()
    return [e for e in events if lo <= str(e.get("date", "")) <= hi]


def _write_cache(path: Path, venue: dict, events: list[dict], df: date, dt: date, method: str | None) -> None:
    cache.save_json(path, {
        "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "venue": venue["name"],
        "events_url": _calendar_url(venue),
        "events": events,
        "window_from": df.isoformat(),
        "window_to": dt.isoformat(),
        "scraper": method,
    })


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--location")
    g.add_argument("--city-key")
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--force", action="store_true", help="Ignore calendar cache freshness.")
    ap.add_argument("--ttl-days", type=float, default=CALENDAR_TTL_DAYS)
    ap.add_argument("--only", help="Only venues whose name contains this text (case-insensitive).")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--venue-budget", type=float, default=75.0, help="Seconds allowed per venue.")
    ap.add_argument("--budget", type=float, default=150.0, help="Overall seconds before giving up on stragglers.")
    ap.add_argument("--out", default=str(DEFAULT_EVENTS_FILE), help="Where to write the aggregated events array.")
    args = ap.parse_args()

    try:
        df, dt = date.fromisoformat(args.date_from), date.fromisoformat(args.date_to)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": f"Bad date: {e}"}))
        return 1
    if df > dt:
        print(json.dumps({"ok": False, "error": "--from must be <= --to"}))
        return 1

    ck = args.city_key or to_city_key(args.location)
    vpath = cache.venue_list_path(ck)
    vdata = _load(vpath)
    if not vdata or not vdata.get("venues"):
        print(json.dumps({"ok": False, "error": f"No venue list at {vpath}. Run venue discovery first."}))
        return 1
    venues = vdata["venues"]
    if args.only:
        venues = [v for v in venues if args.only.lower() in v["name"].lower()]

    t0 = time.monotonic()
    cache.ensure_cache_dir()
    fetcher = Fetcher()

    rows: dict[str, dict] = {}
    todo: list[dict] = []
    for v in venues:
        url = _calendar_url(v)
        if not url:
            rows[v["name"]] = {"name": v["name"], "status": "failed", "reason": "venue has no url",
                               "events_in_window": 0}
            continue
        path = cache.venue_calendar_path(url)
        st = _status(path, args.ttl_days)
        data = _load(path) if st["exists"] else None
        row = {"name": v["name"], "events_url": url, "cache_path": str(path)}
        if not args.force and not st["stale"] and _cache_covers(data, dt):
            row.update(status="cached", method=(data or {}).get("scraper"),
                       events_in_window=len(_in_window((data or {}).get("events", []), df, dt)))
            rows[v["name"]] = row
        else:
            rows[v["name"]] = row
            todo.append(v)

    # Scrape the stale ones concurrently. Straggler threads can't be killed,
    # so we stop waiting at --budget and report them as timeouts.
    ex = ThreadPoolExecutor(max_workers=max(1, min(args.workers, len(todo) or 1)))
    futs = {ex.submit(scrape_venue, v, df, dt, fetcher, args.venue_budget): v for v in todo}
    done, pending = wait(futs, timeout=args.budget)
    for f, v in futs.items():
        row = rows[v["name"]]
        if f in pending:
            row.update(status="failed", reason=f"timed out after {args.budget:.0f}s", events_in_window=0)
            continue
        try:
            res = f.result()
        except Exception as e:  # a parser bug must not sink the whole run
            row.update(status="failed", reason=f"scraper error: {type(e).__name__}: {e}", events_in_window=0)
            continue
        row.update(status=res.status, method=res.method, reason=res.reason)
        row["events_in_window"] = len(_in_window(res.events, df, dt))
        if res.events:
            row["last_event_date"] = max(e["date"] for e in res.events)
        if res.status in ("ok", "partial"):
            _write_cache(Path(row["cache_path"]), v, res.events, df, dt, res.method)
    ex.shutdown(wait=False, cancel_futures=True)
    fetcher.close()

    # Aggregate everything in-window from the (now fresh) caches.
    all_events: list[dict] = []
    for v in venues:
        row = rows[v["name"]]
        if "cache_path" not in row:
            continue
        data = _load(Path(row["cache_path"])) if Path(row["cache_path"]).exists() else None
        if data:
            all_events.extend(_in_window(data.get("events", []), df, dt))
    all_events.sort(key=lambda e: (e["date"], e.get("time") or "", e.get("venue") or ""))
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_events, indent=2, ensure_ascii=False), encoding="utf-8")

    ordered = [rows[v["name"]] for v in venues]
    # A freshly failed venue may still have usable (stale) cache events -- keep
    # them in the aggregate, but still tell the LLM about the failure.
    needs_llm = [r["name"] for r in ordered if r["status"] in ("failed", "partial")]
    counts = {s: sum(1 for r in ordered if r["status"] == s) for s in ("ok", "partial", "failed", "cached")}
    print(json.dumps({
        "ok": True,
        "city_key": ck,
        "date_from": args.date_from,
        "date_to": args.date_to,
        "elapsed_seconds": round(time.monotonic() - t0, 1),
        "events_file": str(out_path),
        "total_events_in_window": len(all_events),
        "venue_counts": counts,
        "needs_llm": needs_llm,
        "venues": ordered,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
