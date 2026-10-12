"""Orchestration tests: strategy dispatch, normalization, status reporting,
and the scrape_calendars CLI's cache behavior. No network: a fake fetcher
serves the saved fixtures by URL."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import cache, venue_scrape as vs
from scripts import scrape_calendars as sc

FIX = Path(__file__).parent / "fixtures"
WIN_FROM, WIN_TO = date(2026, 10, 12), date(2026, 11, 12)


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeFetcher:
    """Maps URL -> body (or an Exception to raise)."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, deadline=None):
        self.calls.append(url)
        v = self.pages.get(url)
        if v is None:
            raise vs.FetchError("http 404")
        if isinstance(v, Exception):
            raise v
        return v, url

    def close(self):
        pass


def venue(name="Test Venue", url="https://v.example/events", **extra):
    return {"name": name, "url": url, "events_url": url, **extra}


# ── strategy dispatch ────────────────────────────────────────────────────

def test_rhp_page_is_detected_and_paginates_until_window_end():
    page2 = read("rhp_sinkhole.html").replace("2026", "2026").replace("Wed, Oct 14", "Wed, Dec 16")
    f = FakeFetcher({
        "https://v.example/events": read("rhp_sinkhole.html"),
        "https://v.example/events/page/2/": page2,
    })
    res = vs.scrape_venue(venue(), WIN_FROM, WIN_TO, f)
    assert res.status == "ok" and res.method == "rhp-events"
    assert "https://v.example/events/page/2/" in f.calls
    assert len(res.events) >= 3


def test_events_written_in_standard_cache_shape():
    f = FakeFetcher({"https://v.example/events": read("bluestrawberry.html")})
    res = vs.scrape_venue(venue("Blue Strawberry"), WIN_FROM, WIN_TO, f)
    assert res.status == "ok" and res.method == "fullcalendar-var"
    e = res.events[0]
    assert set(e) <= {"date", "time", "artists", "venue", "venue_url", "ticket_url"}
    assert e["venue"] == "Blue Strawberry" and e["venue_url"] == "https://v.example/events"
    assert e["artists"] == ["Jane Monheit"]


def test_tribe_rest_with_include_venues_filter():
    f = FakeFetcher({
        "https://blueberryhill.com/shows/": "<html>tribe-events</html>",
        "https://blueberryhill.com/wp-json/tribe/events/v1/events?start_date=2026-10-12&end_date=2026-11-12&per_page=50&page=1":
            read("tribe_blueberryhill.json"),
    })
    v = venue("Blueberry Hill / Duck Room", "https://blueberryhill.com/shows/",
              scrape={"include_venues": ["Duck Room"]})
    res = vs.scrape_venue(v, WIN_FROM, WIN_TO, f)
    assert res.method == "tribe-rest" and res.status == "ok"
    assert [e["artists"][0] for e in res.events] == [
        "Stanley Simmons", "THE SCHIZOPHONICS", "Igor & The Red Elvises", "SOUNDHAUS"]


def test_shared_calendar_split_by_venue_label():
    f = FakeFetcher({"https://p.example/calendar/": read("pageant.html")})
    pageant = vs.scrape_venue(venue("The Pageant", "https://p.example/calendar/",
                                    scrape={"include_venues": ["The Pageant"]}), WIN_FROM, WIN_TO, f)
    delmar = vs.scrape_venue(venue("Delmar Hall", "https://p.example/calendar/#delmar",
                                   scrape={"url": "https://p.example/calendar/", "include_venues": ["Delmar Hall"]}),
                             WIN_FROM, WIN_TO, f)
    assert [e["artists"][0] for e in pageant.events] == ["Suki Waterhouse"]
    assert [e["artists"][0] for e in delmar.events] == ["INOHA"]
    assert delmar.events[0]["venue"] == "Delmar Hall"


def test_ics_feed_discovered_from_page_link_and_noise_filtered():
    f = FakeFetcher({
        "https://cw.example/events": '<a href="https://cw.example/feed/events.ics">cal</a>',
        "https://cw.example/feed/events.ics": read("citywinery.ics"),
    })
    res = vs.scrape_venue(venue("City Winery", "https://cw.example/events"), WIN_FROM, WIN_TO, f)
    assert res.method == "ics-feed"
    names = [e["artists"][0] for e in res.events]
    assert "Keiko Matsui" in names
    assert not any("Vinofile" in n for n in names)  # wine-club pickup party is not a gig


def test_eventon_rest_plus_jsonld_detail_pages():
    posts = json.loads(read("eventon_list.json"))
    pages = {
        "https://k.example/calendar/": "<html>eventon</html>",
        "https://k.example/wp-json/wp/v2/ajde_events?per_page=100": json.dumps(posts),
    }
    for p in posts:
        pages[p["link"]] = read("eventon_detail.html")
    res = vs.scrape_venue(venue("Dark Room", "https://k.example/calendar/",
                                scrape={"include_locations": ["the-dark-room"]}), WIN_FROM, WIN_TO, FakeFetcher(pages))
    assert res.method == "eventon-rest+jsonld" and res.status == "ok"
    # both Dark Room posts return the same JSON-LD in this fixture -> deduped
    assert len(res.events) == 1 and res.events[0]["time"] == "21:00"


def test_eventon_detail_failure_marks_partial():
    posts = json.loads(read("eventon_list.json"))
    pages = {
        "https://k.example/calendar/": "<html>eventon</html>",
        "https://k.example/wp-json/wp/v2/ajde_events?per_page=100": json.dumps(posts),
        posts[0]["link"]: read("eventon_detail.html"),
        posts[1]["link"]: vs.FetchError("http 500"),
    }
    res = vs.scrape_venue(venue("Dark Room", "https://k.example/calendar/",
                                scrape={"include_locations": ["the-dark-room"]}), WIN_FROM, WIN_TO, FakeFetcher(pages))
    assert res.status == "partial" and res.events


# ── failure reporting ────────────────────────────────────────────────────

def test_fetch_error_is_failed_with_reason():
    res = vs.scrape_venue(venue(), WIN_FROM, WIN_TO,
                          FakeFetcher({"https://v.example/events": vs.FetchError("DNS lookup failed (dead domain?)")}))
    assert res.status == "failed" and "DNS" in res.reason and res.events == []


def test_unrecognized_page_is_failed_not_empty_ok():
    res = vs.scrape_venue(venue(), WIN_FROM, WIN_TO,
                          FakeFetcher({"https://v.example/events": "<html><body>Hello, calendar is an image.</body></html>"}))
    assert res.status == "failed" and "no recognizable" in res.reason


def test_recognized_markup_with_zero_events_is_failed():
    html = '<html><div class="show-slug"></div> show-start-date</html>'
    res = vs.scrape_venue(venue(), WIN_FROM, WIN_TO, FakeFetcher({"https://v.example/events": html}))
    assert res.status == "failed" and "parsed 0 events" in res.reason


def test_normalize_drops_past_dupes_and_noise():
    raw = [
        {"date": "2026-10-01", "time": None, "title": "Old", "artists": ["Old"], "ticket_url": None, "venue": None},
        {"date": "2026-10-20", "time": "20:00", "title": "Band", "artists": ["Band"], "ticket_url": None, "venue": None},
        {"date": "2026-10-20", "time": "20:00", "title": "Band", "artists": ["band"], "ticket_url": None, "venue": None},
        {"date": "2026-10-21", "time": None, "title": "Tuesday Trivia", "artists": ["Tuesday Trivia"], "ticket_url": None, "venue": None},
        {"date": "2026-10-22", "time": None, "title": "Comedy Night", "artists": ["X"], "ticket_url": None, "venue": None},
    ]
    out = vs._normalize(raw, venue(scrape={"exclude_title": "comedy"}), {"exclude_title": "comedy"}, WIN_FROM)
    assert [e["artists"] for e in out] == [["Band"]]


# ── scrape_calendars CLI ─────────────────────────────────────────────────

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "VENUES_DIR", tmp_path / "venues")
    monkeypatch.setattr(cache, "CALENDARS_DIR", tmp_path / "calendars")
    cache.ensure_cache_dir()
    venues = {"city_key": "test-city", "venues": [
        {"name": "Good", "url": "https://good.example/", "events_url": "https://good.example/events"},
        {"name": "Bad", "url": "https://bad.example/", "events_url": "https://bad.example/events"},
    ]}
    cache.save_json(cache.venue_list_path("test-city"), venues)
    calls = []

    def fake_scrape(v, df, dt, fetcher, budget):
        calls.append(v["name"])
        if v["name"] == "Bad":
            return vs.ScrapeResult("failed", reason="http 403")
        ev = [{"date": "2026-10-20", "time": "20:00", "artists": ["Band"], "venue": "Good",
               "venue_url": v["events_url"]},
              {"date": "2027-03-01", "artists": ["Far Future"], "venue": "Good", "venue_url": v["events_url"]}]
        return vs.ScrapeResult("ok", ev, "jsonld")

    monkeypatch.setattr(sc, "scrape_venue", fake_scrape)
    monkeypatch.setattr(sc, "Fetcher", lambda: FakeFetcher({}))
    return tmp_path, calls


def run_cli(tmp_path, capsys, *extra):
    out = tmp_path / "events.json"
    argv = ["scrape_calendars.py", "--city-key", "test-city", "--from", "2026-10-12", "--to", "2026-11-12",
            "--out", str(out), *extra]
    old = sys.argv
    sys.argv = argv
    try:
        assert sc.main() == 0
    finally:
        sys.argv = old
    return json.loads(capsys.readouterr().out), json.loads(out.read_text(encoding="utf-8"))


def test_cli_writes_standard_cache_and_reports_per_venue_status(sandbox, capsys):
    tmp, calls = sandbox
    summary, events = run_cli(tmp, capsys)
    rows = {v["name"]: v for v in summary["venues"]}
    assert rows["Good"]["status"] == "ok" and rows["Good"]["events_in_window"] == 1
    assert rows["Bad"]["status"] == "failed" and rows["Bad"]["reason"] == "http 403"
    assert summary["needs_llm"] == ["Bad"]
    # cache file: standard keys at the standard hashed path, nothing written for the failure
    p = cache.venue_calendar_path("https://good.example/events")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert {"fetched_at", "venue", "events_url", "events"} <= set(data)
    assert not cache.venue_calendar_path("https://bad.example/events").exists()
    # aggregate holds only in-window events
    assert [e["artists"] for e in events] == [["Band"]]


def test_cli_uses_fresh_cache_and_force_refetches(sandbox, capsys):
    tmp, calls = sandbox
    run_cli(tmp, capsys)
    calls.clear()
    summary, _ = run_cli(tmp, capsys)
    rows = {v["name"]: v for v in summary["venues"]}
    assert rows["Good"]["status"] == "cached" and rows["Good"]["events_in_window"] == 1
    assert calls == ["Bad"]  # failed venue has no cache -> retried, good one not
    calls.clear()
    run_cli(tmp, capsys, "--force")
    assert sorted(calls) == ["Bad", "Good"]


def test_cli_widened_window_invalidates_narrow_cache(sandbox, capsys):
    tmp, calls = sandbox
    run_cli(tmp, capsys)
    calls.clear()
    old = sys.argv
    sys.argv = ["x", "--city-key", "test-city", "--from", "2026-10-12", "--to", "2026-12-31",
                "--out", str(tmp / "e2.json"), "--only", "Good"]
    try:
        sc.main()
    finally:
        sys.argv = old
    capsys.readouterr()
    assert calls == ["Good"]


def test_cli_hand_written_cache_without_window_is_trusted(sandbox, capsys):
    tmp, calls = sandbox
    p = cache.venue_calendar_path("https://bad.example/events")
    cache.save_json(p, {"fetched_at": "now", "venue": "Bad", "events_url": "https://bad.example/events",
                        "events": [{"date": "2026-10-25", "artists": ["Manual Act"], "venue": "Bad"}]})
    summary, events = run_cli(tmp, capsys)
    rows = {v["name"]: v for v in summary["venues"]}
    assert rows["Bad"]["status"] == "cached" and "Bad" not in calls
    assert ["Manual Act"] in [e["artists"] for e in events]


def test_cli_missing_venue_list(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(cache, "VENUES_DIR", tmp_path / "venues")
    monkeypatch.setattr(cache, "CALENDARS_DIR", tmp_path / "calendars")
    old = sys.argv
    sys.argv = ["x", "--city-key", "nowhere", "--from", "2026-10-12", "--to", "2026-11-12"]
    try:
        assert sc.main() == 1
    finally:
        sys.argv = old
    assert json.loads(capsys.readouterr().out)["ok"] is False
