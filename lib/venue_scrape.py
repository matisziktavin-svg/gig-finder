"""Fetch + extract one venue's calendar (no LLM involved).

``scrape_venue`` picks an extraction strategy from the page itself:

  1. a platform signature we have a parser for (RHP, Webflow shows, Pageant
     theme, TicketWeb plugin, g52 cards, FullCalendar JSON var, Oyster Bar),
  2. The Events Calendar (Tribe) REST API,
  3. EventON REST + per-event JSON-LD,
  4. an advertised .ics feed,
  5. schema.org JSON-LD Event data,
  6. (only if FIRECRAWL_API_KEY is set) the same again on Firecrawl-rendered HTML.

Per-venue overrides live in the venue list entry under ``"scrape"``:

  {"url": "...",                 # page to fetch instead of events_url
   "include_venues": ["Duck Room"],   # keep events whose source venue label
                                      # contains one of these (case-insens.)
   "include_locations": ["the-dark-room"],  # EventON location slugs
   "exclude_title": "regex",     # extra title filter
   "max_pages": 12}
"""
from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlparse, urljoin

import httpx

from lib import venue_parsers as vp

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_MAX_PAGES = 15

# Non-concert noise that shows up on bar/venue calendars.
DEFAULT_EXCLUDE_TITLE = re.compile(
    r"\b(trivia|bingo|karaoke|vinofile|wine tasting|brunch|private event|"
    r"closed for|pick-?up party|paint(?:ing)? (?:and|&) sip)\b",
    re.I,
)


class FetchError(Exception):
    """A fetch failed for a reason worth reporting (http status, dns, ...)."""


@dataclass
class ScrapeResult:
    status: str                      # ok | partial | failed
    events: list[dict] = field(default_factory=list)
    method: str | None = None
    reason: str | None = None
    notes: list[str] = field(default_factory=list)


class Fetcher:
    """Thread-safe httpx wrapper: browser UA, sane timeouts, one retry,
    per-run URL cache, per-venue deadline."""

    def __init__(self, timeout: float = 15.0):
        self.client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/json,text/calendar;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=httpx.Timeout(timeout, connect=8.0),
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[str, str]] = {}

    def close(self) -> None:
        self.client.close()

    def get(self, url: str, deadline: float | None = None) -> tuple[str, str]:
        """Return (body_text, final_url). Raises FetchError."""
        if url in self._cache:
            return self._cache[url]
        last: Exception | None = None
        for attempt in range(2):
            if deadline and time.monotonic() > deadline:
                raise FetchError("venue time budget exceeded")
            try:
                r = self.client.get(url)
            except httpx.ConnectError as e:
                msg = str(e)
                if any(s in msg for s in ("getaddrinfo", "Name or service", "11001", "nodename", "Errno 11")):
                    raise FetchError("DNS lookup failed (dead domain?)") from e
                last = e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
            else:
                if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    last = FetchError(f"http {r.status_code}")
                    time.sleep(1.0)
                    continue
                if r.status_code >= 400:
                    raise FetchError(f"http {r.status_code}")
                self._cache[url] = (r.text, str(r.url))
                return self._cache[url]
            time.sleep(0.5)
        raise FetchError(f"{type(last).__name__}: {last}" if not isinstance(last, FetchError) else str(last))


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _fc_render(url: str) -> str | None:
    """Firecrawl-rendered HTML, only when a key is configured."""
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return None
    try:
        r = httpx.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={"url": url, "formats": ["rawHtml"], "waitFor": 3000},
            timeout=60,
        )
        r.raise_for_status()
        return (r.json().get("data") or {}).get("rawHtml")
    except Exception:
        return None


# ── paginated page-based sources ──────────────────────────────────────────

def _page_url(base: str, n: int, html: str = "") -> str:
    p = urlparse(base)
    if "sf_paged=2" in html:  # Search & Filter Pro (The Pageant): /page/N/ is ignored
        return p._replace(query=f"sf_paged={n}", fragment="").geturl()
    path = p.path if p.path.endswith("/") else p.path + "/"
    path = re.sub(r"/page/\d+/$", "/", path)
    return p._replace(path=f"{path}page/{n}/", query="", fragment="").geturl()


def _paginate(fetcher: Fetcher, first_html: str, base_url: str, parse, win_to: date,
              max_pages: int, deadline: float | None, notes: list[str]) -> tuple[list[dict], bool]:
    """Parse page 1, then /page/N/ until we pass the window or run out.
    Returns (events, truncated)."""
    events = list(parse(first_html))
    seen = {(e["date"], e["title"]) for e in events}
    html = first_html
    n = 1
    while n < max_pages and vp.has_page_link(html, n + 1):
        if events and max(e["date"] for e in events) > win_to.isoformat():
            return events, False
        n += 1
        try:
            html, _ = fetcher.get(_page_url(base_url, n, first_html), deadline)
        except FetchError as e:
            notes.append(f"page {n}: {e}")
            return events, True
        new = [e for e in parse(html) if (e["date"], e["title"]) not in seen]
        if not new:
            return events, False
        for e in new:
            seen.add((e["date"], e["title"]))
        events.extend(new)
    truncated = n >= max_pages and vp.has_page_link(html, n + 1) and \
        (not events or max(e["date"] for e in events) <= win_to.isoformat())
    if truncated:
        notes.append(f"stopped at page cap ({max_pages})")
    return events, truncated


# ── strategy runners ──────────────────────────────────────────────────────

def _try_page_parsers(fetcher, html, final_url, win_from, win_to, cfg, deadline):
    """Signature-based parsers. Returns (method, events, truncated, notes) or None."""
    notes: list[str] = []
    max_pages = int(cfg.get("max_pages", DEFAULT_MAX_PAGES))
    ref = win_from
    if vp.looks_like_rhp(html):
        ev, tr = _paginate(fetcher, html, final_url, lambda h: vp.rhp_events(h, ref), win_to, max_pages, deadline, notes)
        return "rhp-events", ev, tr, notes
    if vp.looks_like_webflow_shows(html):
        return "webflow-shows", vp.webflow_show_events(html, final_url, ref), False, notes
    if vp.looks_like_pageant(html):
        ev, tr = _paginate(fetcher, html, final_url, lambda h: vp.pageant_events(h, ref), win_to, max_pages, deadline, notes)
        return "pageant-theme", ev, tr, notes
    if vp.looks_like_ticketweb_plugin(html):
        ev, tr = _paginate(fetcher, html, final_url, lambda h: vp.ticketweb_plugin_events(h, ref), win_to, max_pages, deadline, notes)
        return "ticketweb-plugin", ev, tr, notes
    if vp.looks_like_g52(html):
        return "g52-cards", vp.g52_events(html, ref), False, notes
    if vp.looks_like_fullcalendar_var(html):
        return "fullcalendar-var", vp.fullcalendar_var_events(html, ref), False, notes
    if vp.looks_like_oyster_bar(html):
        return "oyster-bar", vp.oyster_bar_events(html, ref), False, notes
    return None


def _tribe(fetcher, html, final_url, win_from, win_to, deadline):
    if "tribe-events" not in html and "the-events-calendar" not in html:
        return None
    notes: list[str] = []
    base = f"{_origin(final_url)}/wp-json/tribe/events/v1/events"
    out: list[dict] = []
    page, total_pages = 1, 1
    truncated = False
    while page <= total_pages and page <= 20:
        url = f"{base}?start_date={win_from.isoformat()}&end_date={win_to.isoformat()}&per_page=50&page={page}"
        try:
            body, _ = fetcher.get(url, deadline)
            import json as _json
            payload = _json.loads(body)
        except (FetchError, ValueError) as e:
            if page == 1:
                return None
            notes.append(f"tribe page {page}: {e}")
            truncated = True
            break
        total_pages = int(payload.get("total_pages") or 1)
        out.extend(vp.tribe_events(payload, win_from))
        page += 1
    return "tribe-rest", out, truncated, notes


def _eventon(fetcher, html, final_url, win_from, win_to, cfg, deadline):
    if "eventon" not in html.lower():
        return None
    notes: list[str] = []
    url = f"{_origin(final_url)}/wp-json/wp/v2/ajde_events?per_page=100"
    try:
        body, _ = fetcher.get(url, deadline)
        import json as _json
        posts = _json.loads(body)
    except (FetchError, ValueError):
        return None
    wanted = [s.lower() for s in cfg.get("include_locations", [])]
    links: list[str] = []
    for p in posts:
        classes = p.get("class_list") or []
        if wanted and not any(w in c for c in classes for w in wanted):
            continue
        if not wanted and "event_type_2-music-events" not in classes:
            continue
        links.append(p["link"])
    out: list[dict] = []
    truncated = False

    def one(link: str):
        b, _ = fetcher.get(link, deadline)
        return vp.jsonld_events(b, win_from)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [(l, ex.submit(one, l)) for l in links]
        for l, f in futs:
            try:
                out.extend(f.result())
            except FetchError as e:
                truncated = True
                notes.append(f"{l.rsplit('/', 2)[-2]}: {e}")
    return "eventon-rest+jsonld", out, truncated, notes


def _ics(fetcher, html, final_url, win_from, deadline):
    ics_url = vp.find_ics_url(html, final_url)
    if not ics_url:
        return None
    try:
        body, _ = fetcher.get(ics_url, deadline)
    except FetchError:
        return None
    if "BEGIN:VEVENT" not in body:
        return None
    return "ics-feed", vp.ics_events(body, win_from), False, []


def _extract(fetcher, html, final_url, win_from, win_to, cfg, deadline):
    r = _try_page_parsers(fetcher, html, final_url, win_from, win_to, cfg, deadline)
    if r is None:
        r = _tribe(fetcher, html, final_url, win_from, win_to, deadline)
    if r is None:
        r = _eventon(fetcher, html, final_url, win_from, win_to, cfg, deadline)
    if r is None:
        r = _ics(fetcher, html, final_url, win_from, deadline)
    if r is None:
        ev = vp.jsonld_events(html, win_from)
        if ev:
            r = ("jsonld", ev, False, [])
    return r


# ── public entry ──────────────────────────────────────────────────────────

def scrape_venue(venue: dict, win_from: date, win_to: date, fetcher: Fetcher,
                 budget_s: float = 75.0) -> ScrapeResult:
    cfg = venue.get("scrape") or {}
    url = cfg.get("url") or venue.get("events_url") or venue.get("url")
    if not url:
        return ScrapeResult("failed", reason="venue has no url")
    deadline = time.monotonic() + budget_s
    try:
        html, final_url = fetcher.get(url, deadline)
    except FetchError as e:
        return ScrapeResult("failed", reason=str(e))

    got = _extract(fetcher, html, final_url, win_from, win_to, cfg, deadline)
    rendered = False
    if got is None:
        fc = _fc_render(url)
        if fc:
            got = _extract(fetcher, fc, final_url, win_from, win_to, cfg, deadline)
            rendered = got is not None
    if got is None:
        return ScrapeResult(
            "failed",
            reason="no recognizable event data (JS-rendered, image-only, or unknown platform)",
        )

    method, raw, truncated, notes = got
    if rendered:
        method += "+firecrawl"
    events = _normalize(raw, venue, cfg, win_from)
    if not raw:
        return ScrapeResult("failed", method=method,
                            reason=f"recognized {method} markup but parsed 0 events (markup changed?)",
                            notes=notes)
    status = "partial" if truncated else "ok"
    reason = "; ".join(notes) if notes else None
    return ScrapeResult(status, events, method, reason, notes)


def _normalize(raw: list[dict], venue: dict, cfg: dict, win_from: date) -> list[dict]:
    include = [s.lower() for s in cfg.get("include_venues", [])]
    exclude_cfg = re.compile(cfg["exclude_title"], re.I) if cfg.get("exclude_title") else None
    name = venue["name"]
    vurl = venue.get("events_url") or venue.get("url")
    cutoff = win_from.isoformat()
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in raw:
        if include and not any(s in (e.get("venue") or "").lower() for s in include):
            continue
        if e["date"] < cutoff:
            continue
        if DEFAULT_EXCLUDE_TITLE.search(e.get("title") or "") or \
                (exclude_cfg and exclude_cfg.search(e.get("title") or "")):
            continue
        key = (e["date"], e["time"], tuple(a.lower() for a in e["artists"]))
        if key in seen:
            continue
        seen.add(key)
        item = {"date": e["date"]}
        if e.get("time"):
            item["time"] = e["time"]
        item["artists"] = e["artists"]
        item["venue"] = name
        item["venue_url"] = vurl
        if e.get("ticket_url"):
            item["ticket_url"] = e["ticket_url"]
        out.append(item)
    out.sort(key=lambda x: (x["date"], x.get("time") or ""))
    return out
