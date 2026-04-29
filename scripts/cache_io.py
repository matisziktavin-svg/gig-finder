"""Cache path + freshness helper for SKILL.md orchestration.

Usage:
  python scripts/cache_io.py path venues   --city-key boston-ma
  python scripts/cache_io.py path calendar --url https://venue.com/events
  python scripts/cache_io.py status venues   --city-key boston-ma --ttl-days 365
  python scripts/cache_io.py status calendar --url https://...     --ttl-days 1

Output (stdout, JSON):
  path:   { "path": "..." }
  status: { "path": "...", "exists": bool, "age_days": float|null, "stale": bool }
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import cache


def _path_for(kind: str, city_key: str | None, url: str | None) -> Path:
    if kind == "venues":
        if not city_key:
            raise SystemExit("--city-key required for venues")
        return cache.venue_list_path(city_key)
    if kind == "calendar":
        if not url:
            raise SystemExit("--url required for calendar")
        return cache.venue_calendar_path(url)
    raise SystemExit(f"Unknown kind: {kind}")


def _status(p: Path, ttl_days: float) -> dict:
    if not p.exists():
        return {"path": str(p), "exists": False,
                "age_days": None, "stale": True}
    age_days = (time.time() - p.stat().st_mtime) / 86400
    return {
        "path": str(p),
        "exists": True,
        "age_days": round(age_days, 2),
        "stale": age_days > ttl_days,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    for cmd in ("path", "status"):
        sp = sub.add_parser(cmd)
        sp.add_argument("kind", choices=["venues", "calendar"])
        sp.add_argument("--city-key")
        sp.add_argument("--url")
        if cmd == "status":
            sp.add_argument("--ttl-days", type=float, required=True)

    args = ap.parse_args()
    cache.ensure_cache_dir()
    p = _path_for(args.kind, args.city_key, args.url)

    if args.cmd == "path":
        print(json.dumps({"path": str(p)}))
    else:
        print(json.dumps(_status(p, args.ttl_days)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
