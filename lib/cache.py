"""Filesystem cache helpers for gig-finder runtime data at ~/.gigfinder/."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CACHE_DIR = Path.home() / ".gigfinder"
TASTE_PATH = CACHE_DIR / "taste.json"
GEOCODE_CACHE_PATH = CACHE_DIR / "geocode.cache.json"


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    ensure_cache_dir()
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)
