"""Artist universe building, event matching/tagging, and sorting.

Used by scripts/finalize_search.py to turn raw Claude-extracted events
into a filtered, tagged, chronologically sorted list.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from lib import cache, lastfm

# Cache for Last.fm similar-artist lookups (independent of taste cache)
SIMILAR_CACHE_PATH = cache.CACHE_DIR / "similar.json"
SIMILAR_TTL_DAYS = 30


def normalize_artist_name(name: str) -> str:
    """Match key for artist names across data sources."""
    s = name.lower().strip()
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)  # drop trailing "(USA)" etc.
    return s


# ── Similar-artist cache ─────────────────────────────────────────────────

def _load_similar_cache() -> dict:
    return cache.load_json(SIMILAR_CACHE_PATH) or {}


def _save_similar_cache(c: dict) -> None:
    cache.save_json(SIMILAR_CACHE_PATH, c)


def _similar_for(artist: dict, fresh_after_days: int = SIMILAR_TTL_DAYS) -> list[dict]:
    """Return cached or freshly-fetched Last.fm similars for an artist dict."""
    key = artist["name_normalized"]
    cached = _load_similar_cache()
    entry = cached.get(key)
    if entry:
        age_days = (time.time() - entry.get("fetched_at", 0)) / 86400
        if age_days < fresh_after_days:
            return entry.get("similars", [])
    try:
        similars = lastfm.get_similar_artists(
            artist=artist["name"], mbid=artist.get("mbid"), limit=15,
        )
    except Exception:
        similars = []
    cached[key] = {"fetched_at": time.time(), "similars": similars}
    _save_similar_cache(cached)
    return similars


# ── Artist universe ──────────────────────────────────────────────────────

def build_artist_universe(
    taste: dict,
    expansion_top_n: int = 30,
    favorite_score_floor: float = 10.0,
) -> dict[str, dict]:
    """
    Combine taste-profile artists (Tier 1 favorites) with Last.fm similar
    expansion (Tier 2 discoveries) into a unified universe keyed by
    normalized name.

    Each entry: {name, name_normalized, score, tier, source_label, sources[]}
    """
    universe: dict[str, dict] = {}

    # Tier 1: anyone in the taste profile above the score floor
    for a in taste.get("artists", []):
        if a["score"] < favorite_score_floor:
            continue
        key = a["name_normalized"]
        universe[key] = {
            "name": a["name"],
            "name_normalized": key,
            "score": a["score"],
            "tier": 1,
            "tags": a.get("tags", []),
            "source_label": _favorite_label(a),
            "sources": list(a["sources"]),
        }

    # Tier 2: Last.fm similars for top N favorites
    top_artists = sorted(taste.get("artists", []),
                         key=lambda x: x["score"], reverse=True)[:expansion_top_n]
    for src in top_artists:
        for sim in _similar_for(src):
            key = normalize_artist_name(sim["name"])
            if key in universe:
                continue  # already a favorite
            universe[key] = {
                "name": sim["name"],
                "name_normalized": key,
                "score": sim.get("match", 0) * 5,  # similarity 0-1 → 0-5
                "tier": 2,
                "tags": [],
                "source_label": f"Similar to {src['name']}",
                "sources": [f"similar:{src['name']}"],
            }
    return universe


def _favorite_label(a: dict) -> str:
    """Pick the strongest tag for a Tier 1 artist."""
    s = a["sources"]
    if "lastfm_loved" in s:
        return "Loved"
    pc = a.get("playcount_lastfm", 0)
    if pc >= 100:
        return "Top favorite"
    if "spotify_followed" in s:
        return "Followed"
    if "lastfm_top_overall" in s or "lastfm_top_12m" in s or "lastfm_top_1m" in s:
        return "Favorite"
    return "Library"


# ── Event matching + tagging ─────────────────────────────────────────────

def match_event(event: dict, universe: dict[str, dict],
                fuzzy_threshold: int = 88) -> list[dict]:
    """Return universe entries matching any artist in this event's lineup."""
    matches: list[dict] = []
    seen_keys: set[str] = set()
    artists = event.get("artists") or []
    for raw in artists:
        if not raw:
            continue
        key = normalize_artist_name(raw)
        # Exact normalized match first
        u = universe.get(key)
        if u and key not in seen_keys:
            matches.append(u)
            seen_keys.add(key)
            continue
        # Fuzzy fallback
        for ukey, ue in universe.items():
            if ukey in seen_keys:
                continue
            if fuzz.ratio(key, ukey) >= fuzzy_threshold:
                matches.append(ue)
                seen_keys.add(ukey)
                break
    return matches


def tag_event(matches: list[dict], taste: dict) -> tuple[str, list[str]]:
    """Compute primary tag + secondary tags for an event from its matches."""
    if not matches:
        return ("", [])
    matches_sorted = sorted(matches, key=lambda m: (-m["tier"] != -1, -m["score"]))
    # Prefer Tier 1 over Tier 2
    tier1 = [m for m in matches if m["tier"] == 1]
    if tier1:
        primary_match = max(tier1, key=lambda m: m["score"])
        primary = primary_match["source_label"]
    else:
        primary_match = max(matches, key=lambda m: m["score"])
        primary = primary_match["source_label"]

    secondary: list[str] = []
    # Best tag (genre) for the primary match
    if primary_match.get("tags"):
        secondary.append(f"Genre: {primary_match['tags'][0]}")

    # If multiple distinct matches in the lineup, mention the top other one
    others = [m for m in matches if m is not primary_match]
    if others:
        other = max(others, key=lambda m: m["score"])
        if other["source_label"] != primary:
            secondary.append(f"Also: {other['source_label']}")
    return (primary, secondary)


# ── Sort ─────────────────────────────────────────────────────────────────

def sort_events_by_date(events: list[dict]) -> list[dict]:
    def keyf(e: dict) -> str:
        return (e.get("date") or "9999-99-99")
    return sorted(events, key=keyf)
