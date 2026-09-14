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

# Cache for per-artist size + tag lookups, feeding the Tier-3 discovery pass.
# Listeners/tags barely move, so a long TTL keeps repeat scans near-free.
META_CACHE_PATH = cache.CACHE_DIR / "artist_meta.json"
META_TTL_DAYS = 60

# Discovery tier: surface local acts Tavin has NOT heard, but only when their
# genre tags match core taste AND they sit under this listener cap. Calibrated
# 2026-09-14 — his likes cluster < ~135k (Sports Team, his #1, is 146k), while
# the too-big Tier-2 similars start at 1.4M+. 200k leaves headroom below the gap;
# bigger acts are already covered by the Tier-2 similar-artist expansion.
DISCOVERY_LISTENER_CAP = 200_000

# Below this, an artist is treated as noise (misspellings, non-entities with a
# handful of scrobbles) rather than a genuine small act worth surfacing.
DISCOVERY_LISTENER_FLOOR = 1_000

# Tighter inclusion set for discovery ONLY. The broad tags in _GENRE_SAFE_TAGS
# (rock, alternative, alternative rock, classic rock, new wave) are correct for
# *saving* a borderline artist from exclusion, but too permissive for *finding*
# unheard acts — they drag in roots-rock, emo, and post-hardcore. Discovery
# restricts to the indie-specific tags. (Calibrated 2026-09-14 against the STL
# September scan: broad tags produced ~half the shows as false positives.)
_DISCOVERY_GENRE_TAGS: frozenset[str] = frozenset({
    "indie", "indie rock", "indie pop", "dream pop", "jangle pop",
    "garage rock", "shoegaze", "post-punk", "art punk", "power pop",
    "math rock", "britpop", "lo-fi",
})

# Tags that save an artist from genre exclusion — crossover genres adjacent to
# Tavin's core taste. An artist tagged with both "punk" and "indie rock" stays in.
_GENRE_SAFE_TAGS: frozenset[str] = frozenset({
    "rock", "indie", "indie rock", "alternative", "alternative rock",
    "post-punk", "art punk", "new wave", "indie pop", "dream pop",
    "garage rock", "shoegaze", "math rock", "classic rock", "britpop",
    "lo-fi", "jangle pop", "power pop",
})


def _is_genre_excluded(artist: dict, excluded: frozenset[str]) -> bool:
    """Return True if this artist should be filtered by genre preferences.

    Only excludes when the artist has an excluded tag AND none of their tags
    are 'safe' crossover genres. This keeps post-punk / art-punk artists
    (e.g. Sports Team, The Strokes) while dropping pure-punk acts.
    """
    if not excluded:
        return False
    tags_lower = frozenset(t.lower() for t in artist.get("tags", []))
    if not tags_lower & excluded:
        return False  # no excluded tags — keep
    return not bool(tags_lower & _GENRE_SAFE_TAGS)  # excluded unless a safe tag is present


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
    preferences = taste.get("preferences", {})
    excluded_genres = frozenset(
        g.lower() for g in preferences.get("genre_exclusions", [])
    )

    universe: dict[str, dict] = {}

    # Tier 1: anyone in the taste profile above the score floor, minus excluded genres
    for a in taste.get("artists", []):
        if a["score"] < favorite_score_floor:
            continue
        if _is_genre_excluded(a, excluded_genres):
            continue  # pure-punk artist with no rock/indie crossover — skip
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


# ── Tier 3: local genre-adjacent discovery ───────────────────────────────

def _load_meta_cache() -> dict:
    return cache.load_json(META_CACHE_PATH) or {}


def _save_meta_cache(c: dict) -> None:
    cache.save_json(META_CACHE_PATH, c)


def _genre_hit(tags: list[str]) -> str | None:
    """Return the first tag in the tighter discovery inclusion set, or None.
    Same lowercase convention as _is_genre_excluded / _GENRE_SAFE_TAGS."""
    for t in tags:
        if t.lower() in _DISCOVERY_GENRE_TAGS:
            return t
    return None


def discover_from_events(
    events: list[dict],
    universe: dict[str, dict],
    listener_cap: int = DISCOVERY_LISTENER_CAP,
    listener_floor: int = DISCOVERY_LISTENER_FLOOR,
) -> list[dict]:
    """Post-match pass over shows that matched NOTHING in the taste universe.

    For each unheard artist on those lineups, fetch size + tags (cached) and
    keep the artist only when BOTH gates pass: an indie-set genre tag AND
    listeners in [listener_floor, listener_cap]. Artists with no Last.fm
    presence carry no tags, so the genre gate filters them out automatically.

    Returns events shaped like matched events, marked tier 3 / "Discovery".
    """
    meta_cache = _load_meta_cache()
    dirty = False
    out: list[dict] = []
    now = time.time()

    for ev in events:
        picks: list[dict] = []
        for raw in ev.get("artists") or []:
            if not raw:
                continue
            key = normalize_artist_name(raw)
            if key in universe:
                continue  # already a Tier 1/2 match — not a discovery
            entry = meta_cache.get(key)
            fresh = entry and (now - entry.get("fetched_at", 0)) / 86400 < META_TTL_DAYS
            if not fresh:
                try:
                    info = lastfm.get_artist_info(artist=raw)
                except Exception:
                    info = None
                entry = {
                    "fetched_at": now,
                    "listeners": (info or {}).get("listeners", 0),
                    "tags": (info or {}).get("tags", []),
                    "name": (info or {}).get("name", raw),
                }
                meta_cache[key] = entry
                dirty = True
            listeners = entry.get("listeners", 0)
            gtag = _genre_hit(entry.get("tags", []))
            if gtag and listener_floor <= listeners <= listener_cap:
                picks.append({"name": raw, "listeners": listeners, "genre": gtag})

        if picks:
            best = min(picks, key=lambda p: p["listeners"])  # smallest = most "discovery"
            out.append({
                "date": ev["date"],
                "time": ev.get("time"),
                "venue": ev.get("venue"),
                "venue_url": ev.get("venue_url"),
                "artists": ev.get("artists", []),
                "matched_artists": [p["name"] for p in picks],
                "primary_tag": "Discovery",
                "secondary_tags": [
                    f"Genre: {best['genre']}",
                    f"~{best['listeners']:,} listeners",
                ],
                "ticket_url": ev.get("ticket_url"),
                "tier": 3,
            })

    if dirty:
        _save_meta_cache(meta_cache)
    return out


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
