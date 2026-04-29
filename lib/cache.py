"""Filesystem cache helpers for gig-finder runtime data at ~/.gigfinder/."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CACHE_DIR = Path.home() / ".gigfinder"
TASTE_PATH = CACHE_DIR / "taste.json"
GEOCODE_CACHE_PATH = CACHE_DIR / "geocode.cache.json"
VENUES_DIR = CACHE_DIR / "venues"
CALENDARS_DIR = CACHE_DIR / "calendars"


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    VENUES_DIR.mkdir(parents=True, exist_ok=True)
    CALENDARS_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def venue_list_path(city_key: str) -> Path:
    return VENUES_DIR / f"{city_key}.json"


def venue_calendar_path(url: str) -> Path:
    """Per-venue calendar cache path. Keyed by sha256 of the URL so any URL
    maps deterministically to a single file."""
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return CALENDARS_DIR / f"{h}.json"
