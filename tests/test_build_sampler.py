"""Tests for build_sampler: plan/curation/skip logic and dry-run orchestration.

All network access is injected, so these run offline.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import build_sampler as bs
from lib.spotify import _pick_tracks_from_search, _best_artist_match


# ── Fakes ────────────────────────────────────────────────────────────────

def make_resolver(catalog):
    """catalog: {artist_name: [track_uri, ...]}. Unknown names resolve to None."""
    def resolve(name):
        for artist, uris in catalog.items():
            if artist.lower() == name.lower():
                return {
                    "artist": {"id": f"id:{artist}", "name": artist},
                    "tracks": [{"name": f"{artist} song {i}", "uri": u}
                               for i, u in enumerate(uris)],
                }
        return None
    return resolve


# ── parse_candidates ─────────────────────────────────────────────────────

def test_parse_candidates_accepts_strings_and_objects():
    raw = json.dumps([
        "Tigers Jaw",
        {"name": "Jhariah", "show": "Wed 6/10 @ Teragram"},
        {"name": "  ", "show": "blank"},   # dropped: blank name
        "",                                  # dropped
    ])
    out = bs.parse_candidates(raw)
    assert [c["name"] for c in out] == ["Tigers Jaw", "Jhariah"]
    assert out[1]["show"] == "Wed 6/10 @ Teragram"


# ── plan_sampler ─────────────────────────────────────────────────────────

def test_plan_caps_at_max_per_artist():
    resolve = make_resolver({"Tigers Jaw": ["u1", "u2", "u3", "u4"]})
    plan = bs.plan_sampler([{"name": "Tigers Jaw"}], resolve, set(), max_per_artist=2)
    assert plan[0]["status"] == "added"
    assert [t["uri"] for t in plan[0]["tracks"]] == ["u1", "u2"]


def test_plan_skips_known_artists():
    resolve = make_resolver({"The Strokes": ["u1", "u2"]})
    plan = bs.plan_sampler([{"name": "The Strokes"}], resolve,
                           known_names={"strokes"}, max_per_artist=2)
    assert plan[0]["status"] == "skipped_known"
    assert plan[0]["tracks"] == []


def test_plan_marks_not_found():
    resolve = make_resolver({})
    plan = bs.plan_sampler([{"name": "Nobody At All"}], resolve, set())
    assert plan[0]["status"] == "not_found"


def test_plan_marks_no_tracks():
    resolve = make_resolver({"Silent Act": []})
    plan = bs.plan_sampler([{"name": "Silent Act"}], resolve, set())
    assert plan[0]["status"] == "no_tracks"


def test_plan_dedupes_by_normalized_name():
    resolve = make_resolver({"Tigers Jaw": ["u1", "u2"]})
    cands = [{"name": "Tigers Jaw"}, {"name": "tigers jaw"}]
    plan = bs.plan_sampler(cands, resolve, set())
    assert len(plan) == 1


def test_plan_takes_fewer_than_max_when_only_one_track():
    resolve = make_resolver({"One Hit": ["only"]})
    plan = bs.plan_sampler([{"name": "One Hit"}], resolve, set(), max_per_artist=2)
    assert plan[0]["status"] == "added"
    assert [t["uri"] for t in plan[0]["tracks"]] == ["only"]


# ── build_and_apply orchestration ────────────────────────────────────────

def test_dry_run_does_not_apply():
    resolve = make_resolver({"Tigers Jaw": ["u1", "u2"]})
    applied_calls = []

    result = bs.build_and_apply(
        [{"name": "Tigers Jaw"}],
        dry_run=True,
        playlist_name="Mizzix",
        max_per_artist=2,
        market="from_token",
        resolve=resolve,
        known_loader=lambda: set(),
        applier=lambda name, uris: applied_calls.append((name, uris)),
    )
    assert result["applied"] is False
    assert applied_calls == []            # applier never called in dry-run
    assert result["track_uris"] == ["u1", "u2"]


def test_live_run_applies_only_added_tracks():
    resolve = make_resolver({
        "Tigers Jaw": ["u1", "u2"],
        "Jhariah": ["u3", "u4"],
        "The Strokes": ["known1"],   # will be skipped as known
    })
    applied = {}

    result = bs.build_and_apply(
        [{"name": "Tigers Jaw"}, {"name": "Jhariah"}, {"name": "The Strokes"},
         {"name": "Nobody"}],
        dry_run=False,
        playlist_name="Mizzix",
        max_per_artist=2,
        market="from_token",
        resolve=resolve,
        known_loader=lambda: {"strokes"},
        applier=lambda name, uris: applied.update({"name": name, "uris": uris}),
    )
    assert result["applied"] is True
    assert applied["name"] == "Mizzix"
    assert applied["uris"] == ["u1", "u2", "u3", "u4"]  # strokes + nobody excluded


def test_empty_candidates_reports_nothing_and_skips_apply():
    applied_calls = []
    result = bs.build_and_apply(
        [], dry_run=False, playlist_name="Mizzix", max_per_artist=2,
        market="from_token",
        resolve=make_resolver({}), known_loader=lambda: set(),
        applier=lambda name, uris: applied_calls.append(uris),
    )
    assert result["applied"] is False
    assert result["track_uris"] == []
    assert applied_calls == []


def test_all_tracks_excluded_does_not_apply_empty_playlist():
    # Only known/not-found candidates -> nothing to add -> must NOT wipe the playlist
    applied_calls = []
    result = bs.build_and_apply(
        [{"name": "The Strokes"}, {"name": "Ghost Artist"}],
        dry_run=False, playlist_name="Mizzix", max_per_artist=2,
        market="from_token",
        resolve=make_resolver({"The Strokes": ["k1"]}),
        known_loader=lambda: {"strokes"},
        applier=lambda name, uris: applied_calls.append(uris),
    )
    assert result["applied"] is False
    assert applied_calls == []


# ── search-fallback track picker (pure) ──────────────────────────────────

def _sr(name, tid, artist_id="A1", artist_name="Tigers Jaw"):
    return {"name": name, "id": tid, "uri": f"spotify:track:{tid}",
            "artists": [{"name": artist_name, "id": artist_id}]}


def test_picker_prefers_exact_artist_id():
    items = [
        _sr("Cover by someone", "x1", artist_id="OTHER", artist_name="Wrong Band"),
        _sr("Charmer", "t1"),
        _sr("Hum", "t2"),
    ]
    picks = _pick_tracks_from_search(items, "A1", "Tigers Jaw", 2)
    assert [p["id"] for p in picks] == ["t1", "t2"]


def test_picker_falls_back_to_name_when_id_absent():
    items = [_sr("Charmer", "t1", artist_id="ZZZ")]  # id differs, name matches
    picks = _pick_tracks_from_search(items, "A1", "Tigers Jaw", 2)
    assert [p["id"] for p in picks] == ["t1"]


def test_picker_dedupes_and_caps():
    items = [_sr("Charmer", "t1"), _sr("Charmer (again)", "t1"), _sr("Hum", "t2"),
             _sr("Extra", "t3")]
    picks = _pick_tracks_from_search(items, "A1", "Tigers Jaw", 2)
    assert [p["id"] for p in picks] == ["t1", "t2"]


# ── artist match gating (pure) ───────────────────────────────────────────

def _ar(name, aid):
    return {"name": name, "id": aid}


def test_best_match_exact_wins():
    items = [_ar("Wrong", "w"), _ar("Tigers Jaw", "tj")]
    assert _best_artist_match(items, "tigers jaw")["id"] == "tj"


def test_best_match_rejects_garbage():
    items = [_ar("Xeolt", "x")]
    assert _best_artist_match(items, "Zzxqwv Notaband Xyztron") is None


def test_best_match_accepts_close_fuzzy():
    items = [_ar("Beyoncé", "b")]  # accent difference vs plain query
    assert _best_artist_match(items, "Beyonce")["id"] == "b"


def test_best_match_empty():
    assert _best_artist_match([], "Anyone") is None


# ── summary formatting ───────────────────────────────────────────────────

def test_format_summary_lists_each_status():
    resolve = make_resolver({"Tigers Jaw": ["u1", "u2"]})
    result = bs.build_and_apply(
        [{"name": "Tigers Jaw", "show": "Sat 6/7 @ The Regent"},
         {"name": "The Strokes"}, {"name": "Ghost"}],
        dry_run=True, playlist_name="Mizzix", max_per_artist=2,
        market="from_token", resolve=resolve,
        known_loader=lambda: {"strokes"},
        applier=lambda name, uris: None,
    )
    text = bs.format_summary(result, "Mizzix", dry_run=True)
    assert "Tigers Jaw" in text
    assert "The Regent" in text
    assert "skipped" in text.lower()
    assert "not found" in text.lower() or "not_found" in text.lower()
    assert "dry-run" in text.lower()
