"""Parser tests against trimmed fixtures saved from the real St. Louis venue
pages (tests/fixtures/). Offline and deterministic: reference date is fixed."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import venue_parsers as vp

FIX = Path(__file__).parent / "fixtures"
REF = date(2026, 10, 12)


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# ── text helpers ─────────────────────────────────────────────────────────

def test_parse_date_text_variants():
    assert vp.parse_date_text("Oct 14, 2026", REF) == date(2026, 10, 14)
    assert vp.parse_date_text("Wed, Oct 14", REF) == date(2026, 10, 14)
    assert vp.parse_date_text("MONDAY ☆ OCTOBER 12th", REF) == date(2026, 10, 12)
    assert vp.parse_date_text("Tue, Oct 13 2026", REF) == date(2026, 10, 13)


def test_year_rolls_forward_for_past_month():
    # "Jan 23" seen in October means next January, not the one already gone.
    assert vp.parse_date_text("Jan 23", REF) == date(2027, 1, 23)
    # ...but a date a few days back stays in the current year.
    assert vp.parse_date_text("Oct 5", REF) == date(2026, 10, 5)


def test_parse_time():
    assert vp.parse_time("Show: 8:00 pm") == "20:00"
    assert vp.parse_time("7 pm") == "19:00"
    assert vp.parse_time("12:30 am") == "00:30"
    assert vp.parse_time("no time here") is None


def test_time_range_start_borrows_end_meridian():
    assert vp.parse_time_range_start("8:00P - 12:00A / $8 Cover") == "20:00"
    assert vp.parse_time_range_start("3:00 - 5:00P / Free Show!") == "15:00"
    assert vp.parse_time_range_start("11:00 - 2:00P") == "11:00"


def test_split_lineup_cases():
    s = vp.split_lineup
    assert s("Karina Rykman – LATE CHECKOUT TOUR 2026 with Julia Barfield") == ["Karina Rykman", "Julia Barfield"]
    assert s("Teddy and the Rough Riders w/ Hunter Peebles &amp; the Moonlighters, the Conestogas") == [
        "Teddy and the Rough Riders", "Hunter Peebles & the Moonlighters", "the Conestogas"]
    assert s("1% Productions Presents : Emo Nite ") == ["Emo Nite"]
    assert s("TANK AND THE BANGAS: THE LAST BALLOON TOUR 2026") == ["TANK AND THE BANGAS"]
    assert s("He Films The Clouds- The Hope Bleeds Tour 2026") == ["He Films The Clouds"]
    assert s("Bob Schneider with support from Chris Trapper") == ["Bob Schneider", "Chris Trapper"]
    assert s("Headliner", "with A, B") == ["Headliner", "A", "B"]


def test_split_lineup_keeps_comma_names_and_ampersands():
    assert vp.split_lineup("Black Country, New Road") == ["Black Country, New Road"]
    assert vp.split_lineup("Igor & The Red Elvises") == ["Igor & The Red Elvises"]
    assert vp.split_lineup("Jay-Z") == ["Jay-Z"]


# ── per-platform parsers ─────────────────────────────────────────────────

def test_rhp_events():
    html = read("rhp_sinkhole.html")
    assert vp.looks_like_rhp(html)
    ev = vp.rhp_events(html, REF)
    assert [e["date"] for e in ev] == ["2026-10-14", "2026-10-15", "2026-10-16"]
    assert ev[0]["artists"] == ["Teddy and the Rough Riders", "Hunter Peebles & the Moonlighters", "the Conestogas"]
    assert ev[0]["time"] == "20:00"
    assert ev[0]["ticket_url"].startswith("https://www.etix.com/ticket/p/61418345")
    assert vp.has_page_link(html, 2)


def test_webflow_show_events_joins_index_and_cards():
    html = read("webflow_shows.html")
    assert vp.looks_like_webflow_shows(html)
    ev = vp.webflow_show_events(html, "https://www.offbroadwaystl.com/shows", REF)
    assert [e["artists"] for e in ev] == [
        ["Deerhoof", "Holofoil", "Freckles"], ["Jenna Davis", "Ava Liv Mabry"], ["TRIPPP", "Clover"]]
    assert ev[0]["date"] == "2026-10-13" and ev[0]["time"] == "20:00"
    assert ev[0]["ticket_url"] == "https://www.offbroadwaystl.com/shows/deerhoof-13-oct"


def test_pageant_events_keeps_source_venue_label():
    html = read("pageant.html")
    assert vp.looks_like_pageant(html)
    ev = vp.pageant_events(html, REF)
    assert [e["venue"] for e in ev] == [
        "The Pageant", "Blueberry Hill Duck Room", "Delmar Hall", "Blueberry Hill Duck Room"]
    assert ev[0]["artists"] == ["Suki Waterhouse", "Rochelle Jordan", "EMMMA"]
    assert ev[0]["date"] == "2026-10-12"
    assert ev[0]["ticket_url"].startswith("https://www.ticketmaster.com/")


def test_ticketweb_plugin_events():
    html = read("ticketweb_plugin.html")
    assert vp.looks_like_ticketweb_plugin(html)
    ev = vp.ticketweb_plugin_events(html, REF)
    assert ev[0]["artists"] == ["Karina Rykman", "Julia Barfield"]
    assert (ev[0]["date"], ev[0]["time"]) == ("2026-10-13", "20:00")
    assert ev[2]["time"] == "18:00"
    assert "ticketweb.com/event/" in ev[0]["ticket_url"]


def test_g52_events_infers_year_and_splits_support():
    html = read("g52_factory.html")
    assert vp.looks_like_g52(html)
    ev = vp.g52_events(html, REF)
    assert ev[0]["artists"] == ["Interpol", "DIIV"]
    assert ev[0]["date"] == "2026-10-13"
    assert ev[1]["artists"][:2] == ["Taste of Chaos Tour", "Hollywood Undead"]
    assert ev[0]["ticket_url"].startswith("https://www.ticketmaster.com/")


def test_fullcalendar_var_events_skips_canceled():
    html = read("bluestrawberry.html")
    assert vp.looks_like_fullcalendar_var(html)
    ev = vp.fullcalendar_var_events(html, REF)
    assert [e["artists"][0] for e in ev] == ["Jane Monheit", "Jane Monheit", "Nellie McKay"]
    assert ev[0]["time"] == "18:00" and ev[1]["time"] == "20:30"
    assert ev[0]["ticket_url"] == "https://bluestrawberrystl.com/show/ticket/2170"


def test_oyster_bar_events_day_headers_and_times():
    html = read("oysterbar.html")
    assert vp.looks_like_oyster_bar(html)
    ev = vp.oyster_bar_events(html, REF)
    got = [(e["date"], e["time"], e["artists"][0]) for e in ev]
    assert ("2026-10-12", "20:00", "Soulard Blues Band") in got
    assert ("2026-10-13", "21:00", "Andrew Dahle") in got
    # Two acts on the same night get their own times.
    assert ("2026-10-15", "16:00", "Drew Lance & Friends") in got
    assert ("2026-10-15", "20:00", "Rosalie & the Litzsingers") in got


def test_ics_events_unfolds_and_unescapes():
    ev = vp.ics_events(read("citywinery.ics"), REF)
    assert [e["date"] for e in ev] == ["2026-10-14", "2026-10-14", "2026-10-15"]
    keiko = ev[1]
    assert keiko["artists"] == ["Keiko Matsui"] and keiko["time"] == "19:30"
    assert keiko["ticket_url"].startswith("https://tickets.citywinery.com/event/keiko-matsui")
    assert keiko["venue"] == "City Winery St. Louis"
    assert ev[2]["artists"] == ["Bob Schneider", "Chris Trapper"]


def test_find_ics_url():
    html = '<a href="https://awsapi.citywinery.com/feeds/St. Louis/events.ics">sub</a>'
    assert vp.find_ics_url(html, "https://citywinery.com/x").endswith("/events.ics")
    assert vp.find_ics_url("<html></html>", "https://x.com") is None


def test_tribe_events_carries_venue_label():
    ev = vp.tribe_events(json.loads(read("tribe_blueberryhill.json")), REF)
    duck = [e for e in ev if "Duck Room" in e["venue"]]
    assert [e["artists"][0] for e in duck] == ["Stanley Simmons", "THE SCHIZOPHONICS", "Igor & The Red Elvises", "SOUNDHAUS"]
    assert duck[0]["time"] == "20:00"
    assert duck[0]["ticket_url"] == "https://www.etix.com/ticket/p/91429300/"


def test_jsonld_event_with_bad_utc_offset():
    # Kranzberg emits "2026-10-24T21:00-5:00" (invalid ISO offset); we only
    # read the date/time prefix.
    ev = vp.jsonld_events(read("eventon_detail.html"), REF)
    assert len(ev) == 1
    assert (ev[0]["date"], ev[0]["time"]) == ("2026-10-24", "21:00")
    assert ev[0]["artists"] == ["Jawwaad Spann & The Vintage Society"]


def test_jsonld_prefers_performers_and_offers():
    html = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"MusicEvent",'
            '"name":"Night Show","startDate":"2026-11-02T20:00:00-06:00",'
            '"performer":[{"@type":"MusicGroup","name":"Band A"},{"@type":"MusicGroup","name":"Band B"}],'
            '"offers":{"@type":"Offer","url":"https://tix.example/1"},'
            '"location":{"@type":"Place","name":"The Room"}}</script>')
    ev = vp.jsonld_events(html, REF)
    assert ev[0]["artists"] == ["Band A", "Band B"]
    assert ev[0]["ticket_url"] == "https://tix.example/1"
    assert ev[0]["venue"] == "The Room" and ev[0]["time"] == "20:00"


def test_jsonld_graph_without_events_is_empty():
    html = '<script type="application/ld+json">{"@graph":[{"@type":"WebPage","name":"x"}]}</script>'
    assert vp.jsonld_events(html, REF) == []
