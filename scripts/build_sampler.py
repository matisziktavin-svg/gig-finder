"""Build a "Mizzix sampler" Spotify playlist from a curated list of artists.

Given a list of artist names (each optionally tagged with the show they're
playing), this resolves each to its Spotify artist, takes at most N canonical
top tracks per artist, skips any artist the user already knows (followed or on
a liked track), then CLEARS and refills a single playlist (default "Mizzix").

The curation of *which* artists belong here (dropping DJ nights / tribute acts,
substituting a tribute's source artist, keeping only genuinely new-to-you acts)
is done upstream by Mizzix — this script trusts the names it's handed.

Usage:
  # from a JSON file (list of strings or {"name","show"} objects):
  uv run python scripts/build_sampler.py --input picks.json
  # or piped on stdin:
  cat picks.json | uv run python scripts/build_sampler.py
  # preview without touching Spotify:
  uv run python scripts/build_sampler.py --input picks.json --dry-run

Input shape (stdin or --input):
  ["Tigers Jaw", {"name": "Jhariah", "show": "Wed 6/10 @ Teragram Ballroom"}]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import env
from lib.ranking import normalize_artist_name

env.load_env()

DEFAULT_PLAYLIST = "Mizzix"
DEFAULT_MAX_PER_ARTIST = 2
PLAYLIST_DESCRIPTION = (
    "Auto-built by Mizzix: samplers of new-to-you artists playing shows nearby."
)


# ── Input parsing ─────────────────────────────────────────────────────────

def parse_candidates(raw: str) -> list[dict]:
    """Parse stdin/file JSON into [{name, show}]. Accepts bare strings or
    objects. Entries with a blank/missing name are dropped."""
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of artist names or objects.")
    out: list[dict] = []
    for item in data:
        if isinstance(item, str):
            name, show = item, None
        elif isinstance(item, dict):
            name, show = item.get("name", ""), item.get("show")
        else:
            continue
        name = (name or "").strip()
        if not name:
            continue
        out.append({"name": name, "show": show})
    return out


# ── Planning (pure — no network) ──────────────────────────────────────────

def plan_sampler(
    candidates: list[dict],
    resolve: Callable[[str], dict | None],
    known_names: set[str],
    max_per_artist: int = DEFAULT_MAX_PER_ARTIST,
) -> list[dict]:
    """Turn candidates into a plan of {name, show, status, artist_name, tracks}.

    status ∈ {added, skipped_known, not_found, no_tracks}. Candidates that
    normalize to the same name are collapsed to their first occurrence.
    `resolve(name)` returns {"artist": {...}, "tracks": [{uri,...}]} or None.
    """
    plan: list[dict] = []
    seen: set[str] = set()
    for cand in candidates:
        name = cand["name"]
        key = normalize_artist_name(name)
        if key in seen:
            continue
        seen.add(key)
        entry = {"name": name, "show": cand.get("show"),
                 "status": None, "artist_name": None, "tracks": []}

        if key in known_names:
            entry["status"] = "skipped_known"
            plan.append(entry)
            continue

        resolved = resolve(name)
        if not resolved or not resolved.get("artist"):
            entry["status"] = "not_found"
            plan.append(entry)
            continue

        entry["artist_name"] = resolved["artist"].get("name", name)
        tracks = (resolved.get("tracks") or [])[:max_per_artist]
        if not tracks:
            entry["status"] = "no_tracks"
        else:
            entry["status"] = "added"
            entry["tracks"] = tracks
        plan.append(entry)
    return plan


# ── Real Spotify-backed dependencies ──────────────────────────────────────

def make_resolver(market: str, max_per_artist: int) -> Callable[[str], dict | None]:
    """A resolver that hits Spotify via lib.spotify."""
    from lib import spotify

    def resolve(name: str) -> dict | None:
        artist = spotify.search_artist(name)
        if not artist:
            return None
        tracks = spotify.get_artist_sampler_tracks(
            artist, market=market, limit=max_per_artist)
        return {"artist": artist, "tracks": tracks}

    return resolve


def load_known_names() -> set[str]:
    from lib import spotify
    return spotify.get_known_artist_names()


def apply_to_playlist(playlist_name: str, track_uris: list[str]) -> dict:
    from lib import spotify
    pl = spotify.find_or_create_playlist(
        playlist_name, public=False, description=PLAYLIST_DESCRIPTION)
    spotify.replace_playlist_tracks(pl["id"], track_uris)
    return pl


# ── Orchestration ─────────────────────────────────────────────────────────

def build_and_apply(
    candidates: list[dict],
    *,
    dry_run: bool,
    playlist_name: str = DEFAULT_PLAYLIST,
    max_per_artist: int = DEFAULT_MAX_PER_ARTIST,
    market: str = "from_token",
    resolve: Callable[[str], dict | None] | None = None,
    known_loader: Callable[[], set[str]] | None = None,
    applier: Callable[[str, list[str]], Any] | None = None,
) -> dict:
    """Plan the sampler and (unless dry-run) apply it to the playlist.

    The playlist is only mutated when there is at least one track to add, so a
    run that resolves to nothing never wipes an existing "Mizzix" playlist.
    Network-backed deps are injectable for testing.
    """
    if resolve is None:
        resolve = make_resolver(market, max_per_artist)
    if known_loader is None:
        known_loader = load_known_names
    if applier is None:
        applier = apply_to_playlist

    if not candidates:
        return {"plan": [], "track_uris": [], "applied": False,
                "playlist_name": playlist_name, "playlist": None}

    known = known_loader()
    plan = plan_sampler(candidates, resolve, known, max_per_artist)

    track_uris: list[str] = []
    for entry in plan:
        if entry["status"] == "added":
            track_uris.extend(t["uri"] for t in entry["tracks"])

    applied = False
    playlist = None
    if not dry_run and track_uris:
        playlist = applier(playlist_name, track_uris)
        applied = True

    return {"plan": plan, "track_uris": track_uris, "applied": applied,
            "playlist_name": playlist_name, "playlist": playlist}


# ── Reporting ─────────────────────────────────────────────────────────────

def format_summary(result: dict, playlist_name: str, dry_run: bool) -> str:
    plan = result["plan"]
    added = [e for e in plan if e["status"] == "added"]
    skipped = [e for e in plan if e["status"] == "skipped_known"]
    not_found = [e for e in plan if e["status"] == "not_found"]
    no_tracks = [e for e in plan if e["status"] == "no_tracks"]

    lines: list[str] = []
    mode = "DRY-RUN (nothing written to Spotify)" if dry_run else f'Playlist "{playlist_name}"'
    lines.append(f"🎧 Mizzix sampler — {mode}")
    lines.append("")

    if added:
        n_tracks = sum(len(e["tracks"]) for e in added)
        verb = "would add" if dry_run else "added"
        lines.append(f"✅ {verb} {n_tracks} tracks from {len(added)} artist(s):")
        for e in added:
            show = f"  ({e['show']})" if e.get("show") else ""
            lines.append(f"   • {e['artist_name'] or e['name']}{show}")
            for t in e["tracks"]:
                lines.append(f"       – {t.get('name', t['uri'])}")
    else:
        lines.append("✅ No new artists to add.")

    if skipped:
        lines.append("")
        lines.append(f"⏭️  Skipped {len(skipped)} (already in your library/follows): "
                     + ", ".join(e["name"] for e in skipped))
    if not_found:
        lines.append("")
        lines.append(f"❔ Not found on Spotify ({len(not_found)}): "
                     + ", ".join(e["name"] for e in not_found))
    if no_tracks:
        lines.append("")
        lines.append(f"🔇 No top tracks available ({len(no_tracks)}): "
                     + ", ".join(e["name"] for e in no_tracks))

    if not dry_run and result["applied"]:
        lines.append("")
        lines.append(f'Done — "{playlist_name}" now holds this sampler. Give it a listen.')
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    # Emoji in the summary break the Windows console's default cp1252 codec.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    p = argparse.ArgumentParser(description="Build a Mizzix sampler playlist.")
    p.add_argument("--input", help="Path to a JSON file of artist names/objects. "
                                    "Reads stdin if omitted.")
    p.add_argument("--playlist-name", default=DEFAULT_PLAYLIST)
    p.add_argument("--max-per-artist", type=int, default=DEFAULT_MAX_PER_ARTIST)
    p.add_argument("--market", default="from_token")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the picks without touching Spotify.")
    args = p.parse_args(argv)

    raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    raw = raw.strip()
    if not raw:
        print("No artists provided — nothing to sample.")
        return 0
    try:
        candidates = parse_candidates(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Could not parse input: {e}")
        return 1

    if not candidates:
        print("No artists to sample after parsing — nothing to do.")
        return 0

    try:
        result = build_and_apply(
            candidates,
            dry_run=args.dry_run,
            playlist_name=args.playlist_name,
            max_per_artist=args.max_per_artist,
            market=args.market,
        )
    except Exception as e:
        import httpx
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 401:
            print("Spotify auth expired (401) — run `spotify-mcp --auth` and try again.")
            return 2
        print(f"Error building sampler: {e}")
        return 1

    print(format_summary(result, args.playlist_name, args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
