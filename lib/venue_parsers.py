"""Pure parsers that turn a venue's calendar markup/feed into raw events.

Every parser takes already-fetched text (no network) and returns a list of raw
event dicts:

    {"date": "YYYY-MM-DD", "time": "HH:MM" | None, "title": str,
     "artists": [str, ...], "ticket_url": str | None, "venue": str | None}

``venue`` is the venue label *as printed by the source* (used only to filter
multi-room listings such as The Pageant's calendar); the orchestrator in
``lib/venue_scrape.py`` rewrites it to the venue-list name.

Nothing here touches the network so every parser is unit-testable against a
saved fixture (see tests/test_venue_parsers.py).
"""
from __future__ import annotations

import html as _html
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urljoin

# ── small text helpers ────────────────────────────────────────────────────

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = (
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE_TEXT_RE = re.compile(
    _MONTH_RE + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(20\d\d))?",
    re.I,
)
_TIME_RE = re.compile(r"(\d{1,2})(?::(\d\d))?\s*([ap])\.?m?\.?", re.I)


def text(fragment: str) -> str:
    """Strip tags, unescape entities (twice: WordPress double-encodes) and
    collapse whitespace."""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment or "", flags=re.S)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(_html.unescape(s))
    s = s.replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def infer_date(month: int, day: int, ref: date, year: int | None = None) -> date | None:
    """Resolve a month/day (no year) to the occurrence nearest on/after
    ``ref - 30 days``."""
    try:
        if year:
            return date(year, month, day)
        cand = date(ref.year, month, day)
        if cand < ref - timedelta(days=30):
            cand = date(ref.year + 1, month, day)
        return cand
    except ValueError:
        return None


def parse_date_text(s: str, ref: date) -> date | None:
    """'Oct 14, 2026' / 'Wed, Oct 14' / 'October 13th' -> date."""
    m = _DATE_TEXT_RE.search(s or "")
    if not m:
        return None
    month = MONTHS[m.group(1)[:3].lower()]
    year = int(m.group(3)) if m.group(3) else None
    return infer_date(month, int(m.group(2)), ref, year)


def parse_time(s: str) -> str | None:
    """First clock time in ``s`` as 24h 'HH:MM' ('Show: 8:00 pm' -> '20:00')."""
    m = _TIME_RE.search(s or "")
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
    if not (1 <= h <= 12):
        return None
    h = h % 12 + (12 if ap == "p" else 0)
    return f"{h:02d}:{mi:02d}"


def parse_time_range_start(s: str) -> str | None:
    """'8:00P - 12:00A' -> 20:00; '3:00 - 5:00P' -> 15:00 (borrow the end's
    meridian when the start has none)."""
    m = re.search(
        r"(\d{1,2})(?::(\d\d))?\s*([ap])?\.?m?\.?\s*[-–to]+\s*(\d{1,2})(?::(\d\d))?\s*([ap])", s or "", re.I
    )
    if not m:
        return parse_time(s)
    h1, m1 = int(m.group(1)), int(m.group(2) or 0)
    ap1 = (m.group(3) or "").lower()
    h2, ap2 = int(m.group(4)), m.group(6).lower()
    if not ap1:
        ap1 = ap2
        # "11:00 - 2:00P" means 11 AM; "3:00 - 5:00P" means 3 PM.
        if ap2 == "p" and h1 != 12 and h1 > h2:
            ap1 = "a"
    if not (1 <= h1 <= 12):
        return None
    h = h1 % 12 + (12 if ap1 == "p" else 0)
    return f"{h:02d}:{m1:02d}"


# ── lineup cleanup ────────────────────────────────────────────────────────

_PRESENTS_RE = re.compile(r"^.{0,60}?\bpresents?\s*:?\s+", re.I)
_TOUR_TAIL_RE = re.compile(
    r"(?:\s*[-–—]\s+|\s*:\s+)(?=[^-–—:]*\b(?:tour|20\d\d|anniversary|album|release|ep)\b).*$", re.I
)
_LEAD_RE = re.compile(r"^(?:support(?:ed)?\s+(?:by|from)|special guests?|featuring|feat\.?|with)\s+", re.I)
_NOISE_RE = re.compile(
    r"\s*[\(\[]\s*(?:sold out|21\+|18\+|all ages|early show|late show|"
    r"free|ticketed|canceled|cancelled|postponed)[^\)\]]*[\)\]]"
    r"|\s*\*+\s*(?:sold out|canceled|cancelled)\s*\*+"
    r"|^\s*(?:sold out|canceled|cancelled)\s*[:\-–]\s*",
    re.I,
)
_WITH_RE = re.compile(r"\s+(?:with|w/|w\.|\+|feat\.?|featuring|and special guests?)\s+", re.I)
_LIST_RE = re.compile(r"\s*,\s*|\s+//\s+|\s+\|\s+")
_BAR_RE = re.compile(r"\s+//\s+|\s+\|\s+")


def split_lineup(title: str, supports: Iterable[str] | str | None = None) -> list[str]:
    """Turn a listing title (and optional support strings) into an ordered
    artist list: headliner first.

    Conservative on purpose: '&' is NOT a separator (Hunter Peebles & the
    Moonlighters is one band), and tour taglines after a dash are dropped.
    """
    t = text(title)
    t = _NOISE_RE.sub("", t).strip()
    if re.search(r"\bpresents?\b", t, re.I):
        stripped = _PRESENTS_RE.sub("", t, count=1).strip()
        if stripped:
            t = stripped
    # Commas only separate acts *after* a "with"-style marker, so a band like
    # "Black Country, New Road" survives when it's the headliner.
    chunks = [c for c in _WITH_RE.split(t) if c and c.strip()]
    parts: list[str] = []
    for n, c in enumerate(chunks):
        pieces = _BAR_RE.split(c) if n == 0 else _LIST_RE.split(c)
        parts.extend(x for x in pieces if x and x.strip())
    cleaned: list[str] = []
    for p in parts:
        p = _LEAD_RE.sub("", p)
        p = _TOUR_TAIL_RE.sub("", p).strip(" -–—:")
        if p:
            cleaned.append(p)
    if supports:
        if isinstance(supports, str):
            supports = [supports]
        for s in supports:
            s = text(s)
            s = re.sub(r"^(?:with|w/|support(?:ed)? by|special guests?)\s*:?\s+", "", s, flags=re.I)
            for p in re.split(r"\s*,\s*|\s+\+\s+|\s+and\s+(?=[A-Z])", s):
                p = p.strip(" -–—:")
                if p:
                    cleaned.append(p)
    seen: set[str] = set()
    out: list[str] = []
    for a in cleaned:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out


def _ev(d: date | None, time_: str | None, title: str, artists: list[str],
        ticket_url: str | None = None, venue: str | None = None) -> dict | None:
    if d is None or not artists:
        return None
    return {
        "date": d.isoformat(), "time": time_, "title": title,
        "artists": artists, "ticket_url": ticket_url or None, "venue": venue,
    }


# ── schema.org JSON-LD ────────────────────────────────────────────────────

def _walk_ld(node: Any) -> Iterable[dict]:
    if isinstance(node, list):
        for n in node:
            yield from _walk_ld(n)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "itemListElement", "subEvent"):
            if k in node:
                yield from _walk_ld(node[k])
        if "item" in node:
            yield from _walk_ld(node["item"])


def _ld_type_is_event(t: Any) -> bool:
    ts = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.endswith("Event") for x in ts)


def jsonld_events(html: str, ref: date) -> list[dict]:
    out: list[dict] = []
    for m in re.finditer(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = json.loads(_html.unescape(raw))
            except json.JSONDecodeError:
                continue
        for node in _walk_ld(data):
            if not _ld_type_is_event(node.get("@type")):
                continue
            start = str(node.get("startDate") or "")
            dm = re.match(r"(\d{4})-(\d\d)-(\d\d)", start)
            if not dm:
                continue
            d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            tm = re.search(r"T(\d\d):(\d\d)", start)
            time_ = f"{tm.group(1)}:{tm.group(2)}" if tm and tm.group(0) != "T00:00" else None
            name = text(str(node.get("name") or ""))
            perf = node.get("performer") or node.get("performers")
            names: list[str] = []
            if isinstance(perf, dict):
                perf = [perf]
            if isinstance(perf, list):
                for p in perf:
                    n = p.get("name") if isinstance(p, dict) else p
                    if n:
                        names.append(text(str(n)))
            artists = names or split_lineup(name)
            offers = node.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            ticket = (offers or {}).get("url") if isinstance(offers, dict) else None
            ticket = ticket or node.get("url")
            loc = node.get("location")
            venue = loc.get("name") if isinstance(loc, dict) else None
            e = _ev(d, time_, name, artists, ticket, text(venue) if venue else None)
            if e:
                out.append(e)
    return out


# ── iCalendar ─────────────────────────────────────────────────────────────

def _ics_unescape(v: str) -> str:
    return (v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def ics_events(ics: str, ref: date) -> list[dict]:
    lines: list[str] = []
    for ln in ics.replace("\r\n", "\n").split("\n"):
        if ln[:1] in (" ", "\t") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    out: list[dict] = []
    cur: dict | None = None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT":
            if cur is not None:
                e = _ics_to_event(cur)
                if e:
                    out.append(e)
            cur = None
        elif cur is not None and ":" in ln:
            key, _, val = ln.partition(":")
            cur[key.split(";")[0].upper()] = _ics_unescape(val)
    return out


def _ics_to_event(v: dict) -> dict | None:
    m = re.match(r"(\d{4})(\d\d)(\d\d)(?:T(\d\d)(\d\d))?", v.get("DTSTART", ""))
    if not m:
        return None
    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    time_ = f"{m.group(4)}:{m.group(5)}" if m.group(4) and (m.group(4), m.group(5)) != ("00", "00") else None
    title = text(v.get("SUMMARY", ""))
    url = v.get("URL") or None
    if not url:
        um = re.search(r"Ticket URL:\s*(\S+)", v.get("DESCRIPTION", ""))
        url = um.group(1) if um else None
    loc = v.get("LOCATION", "").split(",")[0].strip() or None
    return _ev(d, time_, title, split_lineup(title), url, loc)


def find_ics_url(html: str, base: str) -> str | None:
    m = re.search(r"""(?:href|src)=["']([^"']+\.ics(?:\?[^"']*)?)["']""", html, re.I)
    if not m:
        m = re.search(r"""["'](https?://[^"'\s]+/events?\.ics[^"'\s]*)["']""", html, re.I)
    return urljoin(base, _html.unescape(m.group(1))) if m else None


# ── The Events Calendar (Tribe) REST ──────────────────────────────────────

def tribe_events(payload: dict, ref: date) -> list[dict]:
    out: list[dict] = []
    for ev in payload.get("events", []):
        sd = str(ev.get("start_date") or "")
        dm = re.match(r"(\d{4})-(\d\d)-(\d\d)(?: (\d\d):(\d\d))?", sd)
        if not dm:
            continue
        d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        time_ = f"{dm.group(4)}:{dm.group(5)}" if dm.group(4) and not ev.get("all_day") else None
        title = text(ev.get("title", ""))
        v = ev.get("venue")
        venue = text(v.get("venue", "")) if isinstance(v, dict) else None
        ticket = ev.get("website") or ev.get("url")
        e = _ev(d, time_, title, split_lineup(title), ticket, venue)
        if e:
            out.append(e)
    return out


# ── Rockhouse Partners (RHP) plugin: Old Rock House, The Sinkhole ─────────

_RHP_SPLIT = re.compile(r'<div class\s*=\s*"[^"]*\brhpSingleEvent\b')


def looks_like_rhp(html: str) -> bool:
    return "rhpSingleEvent" in html


def rhp_events(html: str, ref: date) -> list[dict]:
    chunks = _RHP_SPLIT.split(html)[1:]
    out: list[dict] = []
    for c in chunks:
        dm = re.search(r'id="eventDate"[^>]*>(.*?)</div>', c, re.S)
        d = parse_date_text(text(dm.group(1)), ref) if dm else None
        tm = re.search(r'id\s*=\s*"eventTitle"[^>]*>.*?<h\d[^>]*>(.*?)</h\d>', c, re.S)
        title = text(tm.group(1)) if tm else ""
        if not title:
            tm = re.search(r'rhp-event__title[^>]*>(.*?)</h\d>', c, re.S)
            title = text(tm.group(1)) if tm else ""
        sm = re.search(r'id="evSubHead"[^>]*>(.*?)</h\d>', c, re.S)
        sub = text(sm.group(1)) if sm else ""
        dd = re.search(r"eventDoorStartDate.*?<span[^>]*>(.*?)</span>", c, re.S)
        time_ = None
        if dd:
            dtext = text(dd.group(1))
            show = re.search(r"show:?\s*([^/]*)", dtext, re.I)
            time_ = parse_time(show.group(1)) if show else parse_time(dtext)
            if show and not time_:
                time_ = parse_time(dtext)
        tk = re.search(r'href="([^"]*(?:etix|ticketweb|ticketmaster|tixr|eventbrite|dice\.fm|axs|seetickets)[^"]*)"', c)
        url = _html.unescape(tk.group(1)) if tk else None
        if not url:
            u = re.search(r'id\s*=\s*"eventTitle"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*id\s*=\s*"eventTitle"', c)
            url = _html.unescape(next(g for g in u.groups() if g)) if u else None
        e = _ev(d, time_, title, split_lineup(title, sub), url)
        if e:
            out.append(e)
    return out


# ── Webflow "shows" collection: Off Broadway, Fubar Lounge ────────────────

def looks_like_webflow_shows(html: str) -> bool:
    return 'class="show-slug"' in html and "show-start-date" in html


def webflow_show_events(html: str, base_url: str, ref: date) -> list[dict]:
    index: dict[str, tuple[str, date | None]] = {}
    for m in re.finditer(
        r'<div class="show-name">(.*?)</div><div class="show-start-date">(.*?)</div>'
        r'(?:<div class="show-end-date">.*?</div>)?<div class="show-slug">(.*?)</div>', html, re.S
    ):
        index[text(m.group(3))] = (text(m.group(1)), parse_date_text(text(m.group(2)), ref))
    cards: dict[str, tuple[str, str]] = {}  # slug -> (support, show time)
    for m in re.finditer(r'<a href="/shows/([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        body = m.group(2)
        sup = re.search(r'<h3 class="support">(.*?)</h3>', body, re.S)
        show = re.search(r"Show:</div><div[^>]*>(.*?)</div>", body, re.S)
        doors = re.search(r"Doors:</div><div[^>]*>(.*?)</div>", body, re.S)
        cards[m.group(1)] = (
            text(sup.group(1)) if sup else "",
            (text(show.group(1)) if show else "") or (text(doors.group(1)) if doors else ""),
        )
    out: list[dict] = []
    for slug, (name, d) in index.items():
        support, tstr = cards.get(slug, ("", ""))
        e = _ev(d, parse_time(tstr), name, split_lineup(name, support),
                urljoin(base_url, f"/shows/{slug}"))
        if e:
            out.append(e)
    return out


# ── The Pageant / Delmar Hall WordPress theme ─────────────────────────────

def looks_like_pageant(html: str) -> bool:
    return 'class="event-item"' in html and "status-bar" in html


def pageant_events(html: str, ref: date) -> list[dict]:
    out: list[dict] = []
    for c in html.split('<div class="event-item">')[1:]:
        day = re.search(r'<h2 class="day">(\d+)</h2>\s*<p>([A-Za-z]+)</p>', c)
        yr = re.search(r'class="event-year">(\d{4})<', c)
        d = None
        if day:
            mon = MONTHS.get(day.group(2)[:3].lower())
            d = infer_date(mon, int(day.group(1)), ref, int(yr.group(1)) if yr else None) if mon else None
        name = re.search(r'<div class="artist-name">\s*<h3>(.*?)</h3>', c, re.S)
        openers = re.findall(r'<span role="heading"[^>]*>(.*?)</span>', c, re.S)
        sub = re.search(r"<div class='event-subtitle'>\s*<p>(.*?)</p>", c, re.S)
        ven = re.search(r'class="venue-name">.*?<a [^>]*>(.*?)<span', c, re.S)
        tk = re.search(r'class="button buy-ticket" href="([^"]+)"', c)
        mi = re.search(r'class="more-info"', c)
        link = re.search(r'<a href="(https://[^"]+/event/[^"]+)"', c)
        headliner = text(name.group(1)) if name else ""
        artists = [headliner] + [text(o) for o in openers] if headliner else []
        e = _ev(d, None, headliner, [a for a in artists if a],
                _html.unescape(tk.group(1)) if tk else (link.group(1) if link and mi else None),
                text(ven.group(1)) if ven else None)
        if e:
            out.append(e)
    return out


# ── Jamo Presents / TicketWeb WordPress plugin: Atomic Cowboy ─────────────

def looks_like_ticketweb_plugin(html: str) -> bool:
    return "tw-plugin-upcoming-event-list" in html


def ticketweb_plugin_events(html: str, ref: date) -> list[dict]:
    out: list[dict] = []
    for c in html.split('<div class="tw-section">')[1:]:
        nm = re.search(r'class="tw-name">\s*<a [^>]*>(.*?)</a>', c, re.S)
        dt = re.search(r'class="tw-event-date">(.*?)</span>', c, re.S)
        tm = re.search(r'class="tw-event-time">(.*?)</span>', c, re.S) or \
            re.search(r'class="tw-event-door-time">(.*?)</span>', c, re.S)
        vn = re.search(r'class="tw-venue-name">(.*?)</span>', c, re.S)
        tk = re.search(r'class="[^"]*tw-buy-tix-btn[^"]*"', c)
        href = re.search(r'<a [^>]*href="(https://[^"]*ticketweb[^"]*)"', c)
        sub = re.search(r'class="tw-prefix">(.*?)</div>', c, re.S)
        title = text(nm.group(1)) if nm else ""
        d = parse_date_text(text(dt.group(1)), ref) if dt else None
        e = _ev(d, parse_time(text(tm.group(1))) if tm else None, title, split_lineup(title),
                href.group(1) if (href and tk) else None, text(vn.group(1)) if vn else None)
        if e:
            out.append(e)
    return out


# ── Factory (St. Louis) "g52" event-card blocks ───────────────────────────

def looks_like_g52(html: str) -> bool:
    return "data-hero-event=" in html


def g52_events(html: str, ref: date) -> list[dict]:
    out: list[dict] = []
    for raw in re.findall(r'data-hero-event="([^"]*)"', html):
        try:
            ev = json.loads(_html.unescape(raw))
        except json.JSONDecodeError:
            continue
        d = parse_date_text(str(ev.get("date", "")), ref)
        title = text(ev.get("title") or "")
        support = ev.get("support") or ""
        e = _ev(d, parse_time(str(ev.get("doors") or "")), title, split_lineup(title, support),
                ev.get("ticketLink") or ev.get("permalink"))
        if e:
            out.append(e)
    return out


# ── Blue Strawberry: FullCalendar events embedded as a JSON string ────────

def looks_like_fullcalendar_var(html: str) -> bool:
    return "var calendarEvents" in html


def fullcalendar_var_events(html: str, ref: date) -> list[dict]:
    m = re.search(r'var calendarEvents\s*=\s*("(?:[^"\\]|\\.)*")\s*;', html)
    if not m:
        return []
    data = json.loads(m.group(1))
    if isinstance(data, str):
        data = json.loads(data)
    out: list[dict] = []
    for ev in data:
        if "CANCEL" in str(ev.get("show_status") or "").upper():
            continue
        dm = re.match(r"(\d{4})-(\d\d)-(\d\d)(?:[ T](\d\d):(\d\d))?", str(ev.get("start") or ""))
        if not dm:
            continue
        d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        time_ = f"{dm.group(4)}:{dm.group(5)}" if dm.group(4) and not ev.get("allDay") else None
        title = text(ev.get("title") or "")
        e = _ev(d, time_, title, split_lineup(title), ev.get("TicketUrl") or ev.get("DetailUrl"))
        if e:
            out.append(e)
    return out


# ── Broadway Oyster Bar's hand-written music schedule ─────────────────────

def looks_like_oyster_bar(html: str) -> bool:
    return "Gal-title" in html and "Gal-subtitle" in html


def oyster_bar_events(html: str, ref: date) -> list[dict]:
    out: list[dict] = []
    parts = re.split(r'<h1 class="Gal-title">', html)[1:]
    for part in parts:
        head, _, body = part.partition("</h1>")
        d = parse_date_text(text(head), ref)
        # act = a subtitle span optionally followed by its time span.
        for m in re.finditer(
            r'<span class="Gal-subtitle">(.*?)</span>\s*(?:<br\s*/?>\s*)?(?:<span class="Gal-time">(.*?)</span>)?',
            body, re.S,
        ):
            name = text(m.group(1))
            if not name:
                continue
            tstr = text(m.group(2)) if m.group(2) else ""
            e = _ev(d, parse_time_range_start(tstr), name, split_lineup(name))
            if e:
                out.append(e)
        # stop at the next day: parts are already split per day header.
    return out


# ── pagination helper ─────────────────────────────────────────────────────

def has_page_link(html: str, n: int) -> bool:
    return bool(re.search(rf"/page/{n}/|[?&](?:sf_)?paged?={n}\b|[?&]pg={n}\b", html))
