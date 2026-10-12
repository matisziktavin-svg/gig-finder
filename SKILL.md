---
name: gig-finder
description: Find indie / small-venue concerts near a location within a date range, matched against the user's real listening history (Last.fm + Spotify) and expanded with similar artists. Triggers on requests like "find me gigs / shows / concerts in <place> from <date> to <date>", "what's playing in <city> next month", "refresh my taste profile", "show me my taste profile", or "refresh venues for <city>".
---

# gig-finder

A skill that combines deterministic Python helpers with your own web research to surface concerts the user will care about. The skill is **indie-only by design** — large venues (stadiums, arenas, amphitheaters, theaters > ~2000 cap) are explicitly out of scope.

All commands assume the current working directory is the skill directory. Use `uv run python ...` to invoke scripts so they pick up the project's virtualenv.

**find-gigs in one line:** `prepare_search.py` -> `scrape_calendars.py` -> `finalize_search.py` -> render. The scraper does all calendar fetching/extraction deterministically (a few seconds to a minute for a whole city); you only touch venues it reports as `failed`/`partial`, inline, within a short time budget (Step 4). This works the same whether you were launched inline or as a background agent — just never spawn *more* background agents for scraping.

---

## Mode dispatch

Inspect the user's request and pick exactly one mode:

| Phrase pattern | Mode |
|---|---|
| "find / list me gigs / shows / concerts in <place>" + date range | `find-gigs` |
| "what's playing in <place> [during X]" | `find-gigs` |
| "show me my taste profile" / "what does my taste look like" / "describe my music taste" / "who's in my taste profile" | `show-taste` |
| "refresh my taste profile" / "rebuild taste" / "update my music tastes" / "remine my listening" | `refresh-taste` |
| "refresh venues for <city>" / "rediscover venues in <city>" | `refresh-venues` |
| "make / build a playlist / sampler of those [artists]" (after a gig search) | `find-gigs` → Step 7 |

If the user asks something ambiguous, ask one clarifying question before dispatching.

---

## Mode: `find-gigs`

### Step 1 — parse user input

Extract three values from the user's message:
- `location` — freeform string ("Boston", "Brooklyn", "London, UK", "Berlin").
- `date_from`, `date_to` — both `YYYY-MM-DD`. Resolve relative dates (e.g. "next month", "this weekend", "June 1-15") against today's date. If the user gives a single date, use it for both. If they give an open-ended phrase like "this summer", pick a sensible window (e.g. June 1 – Aug 31 of the current year) and tell the user what window you chose.

Reject silently and ask for clarification only if location is missing.

### Step 2 — run `prepare_search.py`

Run:
```bash
uv run python scripts/prepare_search.py \
  --location "<location>" --from <date_from> --to <date_to>
```

Capture the JSON output. If `ok: false`:
- If error is "No taste profile found" → tell the user, run `refresh-taste` first, then continue.
- For other errors: surface to user and stop.

If `ok: true` → use these fields downstream:
- `city_key`, `lat`, `lng`, `date_from`, `date_to`
- `venue_cache_path`, `venue_cache_stale`
- `taste_age_days`, `taste_stale` (if `taste_stale: true`, advise the user to `refresh-taste` but proceed)

### Step 3 — get the venue list (cache or discover)

If `venue_cache_stale` is `false`:
- Read the file at `venue_cache_path` and use that list.

Otherwise (cache is stale or missing), do **venue discovery**:

1. Run **multiple** WebSearch queries to triangulate. Use queries like:
   - `best small independent music venues in <location>`
   - `<location> indie music clubs DIY venues`
   - `<location> live music small venues under 1000 capacity`
   - For specific scenes you suspect (e.g. electronic, jazz, punk): refine further.

2. Read the top results across the searches. Look for editorial guides, local-music blog roundups, "best of" lists, Reddit threads in the city sub. Cross-reference to identify recurring venue names.

3. **Filter aggressively** to indie-scale venues. Exclude:
   - Anything matching `stadium`, `arena`, `amphitheater`, `coliseum`.
   - Theaters / centers larger than ~2000 capacity.
   - Casinos, fairgrounds, festivals (festivals are too dense to mine event-by-event).
   - Ticketing aggregators (Ticketmaster, AXS, See Tickets) — those are not venues.
   - Bars that only occasionally host music — only include if music is a primary offering.

4. **For each kept venue**, you need its official website (NOT a Bandsintown / Songkick / Resident Advisor profile — those are aggregators we can't query reliably). If a search result only links to an aggregator, do another targeted search for the venue's official site (`<venue name> official site` or `<venue name> events calendar`).

5. Assemble up to **20 venues** as a JSON array of objects with these fields:
   ```json
   {
     "name": "The Sinclair",
     "url": "https://sinclaircambridge.com",
     "events_url": "https://sinclaircambridge.com/calendar",
     "neighborhood": "Cambridge",
     "capacity_hint": 525
   }
   ```
   `events_url` is preferred (the calendar page); `url` is the venue homepage. `neighborhood` and `capacity_hint` are optional but helpful — fill if obvious from the page.

   Optional per-venue scraper hints go under a `"scrape"` key (only add one if a venue's `events_url` is not the page that lists the shows, or it shares a page with another venue): `{"url": "<page to fetch instead>", "include_venues": ["Duck Room"]}` (keep only events whose source-venue label contains one of these), `{"include_locations": ["the-dark-room"]}` (EventON), `{"exclude_title": "<regex>"}`. Two venues that share one calendar page need distinct `events_url` values (e.g. add a `#fragment`) so each gets its own cache file.

6. Save the list with the Write tool to `venue_cache_path` (a JSON object with `discovered_at`, `city_key`, `venues`):
   ```json
   {
     "discovered_at": "<ISO timestamp now>",
     "city_key": "<from prepare_search>",
     "venues": [...]
   }
   ```

### Step 4 — scrape the calendars (script first, model only for leftovers)

Calendar scraping is **deterministic and done by a script**, not by you. It fetches every stale venue calendar concurrently, extracts events (schema.org JSON-LD, iCal feeds, The Events Calendar / EventON REST, and the specific WordPress / Webflow / ticketing markup the venues in the city lists actually use), writes the per-venue cache files, and aggregates the in-window events. A full city normally finishes in well under a minute.

```bash
uv run python scripts/scrape_calendars.py \
  --location "<location>" --from <date_from> --to <date_to>
```

Add `--force` to ignore the 24h cache (e.g. "refresh the calendars"), `--only "<venue name>"` to redo one venue. It prints one JSON summary:

- `events_file` — path of the aggregated events array (default `~/.gigfinder/gigfinder_events.json`); feed this to Step 5.
- `venues[]` — per venue `status` (`ok` | `partial` | `failed` | `cached`), `events_in_window`, `method`, `reason`, `last_event_date`.
- `needs_llm` — the venues the script could not fully handle (`failed` = nothing usable, `partial` = a page or detail fetch broke mid-way). **This list is the only place you do any extraction work.**

If `needs_llm` is empty, go straight to Step 5. Do not re-verify venues the script reported `ok` or `cached`.

#### Fallback for `needs_llm` venues (optional, time-boxed)

Hard rules:
- **Inline only.** Do it yourself in this session, or with *foreground* agents you wait on. **Never launch background subagents for scraping**, and never fan venues out across agents — the script already did the parallel part, and background agents are what made earlier runs slow and flaky.
- **Budget: about 5 minutes total** for all fallback venues, roughly 1 minute per venue. One attempt with WebFetch; if the page is an empty JS shell, one `firecrawl_scrape` (`formats: ["markdown"]`, `only_main_content: true`). No retry loops, no paging through 100K-char pages.
- **If a venue still won't yield events inside its minute, skip it and report it** in the final answer ("Heavy Anchor — calendar is an image, not scraped"). A partial answer delivered on time beats a complete one that never arrives.
- Read the `reason` first: `DNS lookup failed` / `http 404` mean the venue URL in the list is probably dead — say so (and offer `refresh-venues`) rather than hunting for the page. `no recognizable event data` usually means a JS-rendered or image-only calendar.
- For `partial` venues, only fill the gap the `reason` / `last_event_date` points to, then stop.

To record what you extracted, write the venue's cache file (path = `cache_path` in the summary) in the standard shape. Leave out `window_from`/`window_to` (that marks it as hand-written and trusted):

```json
{
  "fetched_at": "<ISO now>",
  "venue": "<venue name>",
  "events_url": "<events_url>",
  "events": [
    {
      "date": "YYYY-MM-DD",          // required
      "time": "HH:MM",               // optional, 24h
      "artists": ["Headliner", ...], // required, in lineup order, bare artist names
      "venue": "<venue name>",       // required (name from the venue list)
      "venue_url": "<events_url>",
      "ticket_url": "<deep link>"    // if a per-event ticket URL exists
    }
  ]
}
```

Only in-window events, skip anything without a date or artist. If the page is hard-blocked (403 / Cloudflare), write an empty `events` array so it isn't retried for 24h.

Then **re-run the same `scrape_calendars.py` command** (no `--force`). Fresh caches are not re-fetched; it just rebuilds the aggregate `events_file` including your hand-filled venues. Don't concatenate events by hand.

### Step 5 — match against taste (`finalize_search.py`)

```bash
uv run python scripts/finalize_search.py \
  --from <date_from> --to <date_to> --events-file "<events_file from Step 4>"
```

(It also still accepts the events array on stdin.)

The output JSON has `stats` and `events` (filtered, tagged, sorted). The `events` array is what to render.

Each event carries a `tier`:
- **Tier 1** — a taste-profile favorite is in the lineup (artists the user actually listens to).
- **Tier 2** — a Last.fm "similar artist" of a favorite (adjacent, usually bigger names).
- **Tier 3 / Discovery** — the user has *not* heard this act, but it passed the genre + size gate: an indie-set genre tag AND Last.fm listeners in [1k, 200k]. These are small local acts you'd otherwise skip (e.g. The 4411). `primary_tag` is `"Discovery"` and `secondary_tags` carry the matched genre + a rough listener count. Toggle with `--no-discovery`; adjust the ceiling with `--discovery-cap N`.

### Step 6 — render to the user

Format as a plain markdown list, sorted as given (date ascending). Each entry:

```
**Sat 2026-06-05 · 8pm** — *The Strokes* @ The Sinclair, Cambridge
  [Top favorite] · [Genre: indie rock]
  Tickets: https://...
```

- Use weekday + date in human form.
- Bold the date+time, italicize the headliner (first matched artist, or the first artist if nothing matched).
- Tags from `primary_tag` and `secondary_tags`, joined with ` · ` and wrapped in `[…]`.
- Tickets line only if `ticket_url` is present.
- If multiple matched artists in the lineup, mention them after the headliner: `*Headliner*, with Other Match`.
- **Discovery (Tier 3) events** render the same way — `primary_tag` is `"Discovery"` and `secondary_tags` carry the genre + rough listener count, so they read like `[Discovery · Genre: indie rock · ~12,000 listeners]`. These are new-to-the-user acts; consider grouping them under a short "New to you" subheading below the taste matches if there are several, so the user can tell them apart from artists they already know.
- If `events` is empty, say so plainly and suggest either widening the date range, refreshing venues, or refreshing the taste profile.

End with a one-line summary of `stats` (e.g. "Scanned 18 venues → 47 events in window → 12 matched your taste, 5 discovery picks"). `stats` carries `tier1_count`, `tier2_count`, and `tier3_count` (discovery) separately — surface the discovery count so the user knows how many are unheard acts.

If any venue ended up skipped, failed, or partial after Step 4, add one short line naming them and why (e.g. "Not covered: Heavy Anchor (calendar is a flyer image), Platypus (no listing online)"). Never silently drop a venue.

### Step 7 — offer a "Mizzix sampler" playlist

Whenever you've just surfaced artists the user likely **hasn't listened to** — whether they came from the automated Tier-2 (`Similar to …`) matches, the Tier-3 **Discovery** picks (unheard local acts that passed the genre + size gate — these are new-to-the-user *by definition*), **or** your own manual picks when the scorer returned little/nothing (a big city where venue calendars didn't scrape is the common case) — end with a short offer:

> "Want me to drop a sampler of these into your **Mizzix** playlist so you can hear them before deciding on the show?"

Only offer when there's at least one genuinely new-to-the-user artist. Skip the offer if every match is already a favorite.

If the user says yes (or asks directly, e.g. "make a playlist of those"):

1. **Curate to real, Spotify-searchable recording artists.** This is your judgment call — the script trusts whatever names you hand it:
   - **Keep** touring/recording acts the user probably hasn't heard.
   - **Drop** things that aren't a recording artist you can sample: DJ/club nights, theme nights ("Dark 80s New Wave Nite"), open decks, comedy, etc.
   - **Tribute / covers nights**: substitute the *source* artist only if it makes sense (a Smiths tribute → `The Smiths`) — and only if the user doesn't already know them. Otherwise drop it.
   - Don't bother filtering out artists the user already listens to; the script skips anything already in their library/follows.
2. **Write the curated list** with the Write tool to a temp JSON file — an array of objects `{"name": "...", "show": "Sat 6/7 · 8pm @ The Regent"}` (the `show` string is optional context shown in the summary):
   ```json
   [
     {"name": "Tigers Jaw", "show": "Sat 6/7 · 8pm @ The Regent"},
     {"name": "Jhariah", "show": "Wed 6/10 @ Teragram Ballroom"}
   ]
   ```
3. **Run the builder** (add `--dry-run` first if you want to preview without touching Spotify):
   ```bash
   uv run python scripts/build_sampler.py --input <temp-file>
   ```
4. **Relay the script's summary** (what it added, skipped as already-known, or couldn't find). If it prints the expired-auth message, tell the user their Spotify login expired and to run `spotify-mcp --auth`, then retry.

Behavior to know so you set expectations correctly:
- **One rolling playlist named exactly `Mizzix`.** Each run **replaces** its contents — it's a fresh sampler, not a growing archive. (Spotify's API can't file it into a folder; it lives at the top level.)
- **At most 2 tracks per artist**, so it stays a quick taste-test.
- If your curated list comes out **empty**, say so and don't run the script.
- The script prefers each artist's canonical top tracks, but this Spotify app is in development mode where that endpoint is blocked, so it automatically falls back to a popularity-ranked track search. Picks are still representative; just don't promise "official top 10" ordering.

---

## Mode: `show-taste`

Read the file at `~/.gigfinder/taste.json` directly with the Read tool. If it doesn't exist, tell the user to run `refresh my taste profile` first.

When it exists, render a human-readable summary covering:

1. **Header line** — when the profile was last updated (relative time, e.g. "2 hours ago" / "3 days ago"), and a one-line stats summary: `<total_artists> artists across <total_genres> genres, mined from Last.fm + Spotify in <elapsed_seconds>s`.

2. **Top 15 artists** — from `artists[]` (already sorted by score). For each: rank, name, score, Last.fm playcount if > 0, top tags (first 3 from `tags`). Format like:
   ```
    1. Sports Team (score 91.7, 1159 plays) — post-punk, indie rock, art punk
    2. The Strokes (score 76.8, 367 plays) — indie rock, indie, rock
   ```

3. **Top 15 genres** — from `genres[]` (sorted by weight). Format like:
   ```
    1. indie rock (weight 1645)
    2. indie (weight 1252)
   ```

4. **Source breakdown** — count how many artists have each source label across `artists[].sources[]`. Output the most informative ones:
   - `lastfm_top_overall`, `lastfm_top_12m`, `lastfm_top_1m` — Last.fm tier counts
   - `lastfm_loved` — explicitly loved on Last.fm
   - `spotify_followed` — explicitly followed on Spotify
   - `spotify_liked` — at least one liked song
   - `spotify_playlist` — appears in at least one owned playlist

5. **Freshness hint** — if the profile is older than 30 days, suggest running `refresh-taste`.

Keep the whole output skimmable — don't dump the raw JSON, summarize. End with a one-line offer: "Want to refresh this, or run a `find-gigs` search?"

---

## Mode: `refresh-taste`

Tell the user this can take ~1 minute and run:

```bash
uv run python scripts/refresh_taste.py
```

Surface its final summary lines (top 10 artists, top 10 genres) plus the elapsed time. Then ask if they want you to run a `find-gigs` query.

---

## Mode: `refresh-venues`

Parse the city from the user's request. Compute the cache path:

```bash
uv run python scripts/cache_io.py path venues --city-key "<city_key>"
```

Delete the file at that path (use Bash `rm`). Then proceed as `find-gigs` (which will re-discover the venue list since the cache is gone).

If the user said `refresh venues for <city>` without supplying dates, ask for the date range before doing the find — refreshing alone with no search wastes the discovery work.

---

## Caching policy reference

| What | TTL | Where |
|---|---|---|
| Taste profile | advisory 30 days (warns, does not force) | `~/.gigfinder/taste.json` |
| Per-city venue list | 365 days | `~/.gigfinder/venues/<city_key>.json` |
| Per-venue calendar | 24 hours (and must cover the requested `--to` date; hand-written caches without `window_to` are trusted) | `~/.gigfinder/calendars/<sha256(events_url)[:16]>.json` |
| Aggregated in-window events (output of `scrape_calendars.py`) | rebuilt every run | `~/.gigfinder/gigfinder_events.json` |
| Geocode | forever | `~/.gigfinder/geocode.cache.json` |
| Last.fm similar artists | 30 days | `~/.gigfinder/similar.json` |
| Last.fm artist meta (listeners + tags, for Tier-3 discovery) | 60 days | `~/.gigfinder/artist_meta.json` |

The user can manually delete any of these to force a refresh. `refresh-venues` and `refresh-taste` are the supported commands.

---

## Failure modes

- **No taste profile** → prompt user to run `refresh-taste` first.
- **Geocoding returns nothing** → ask the user to specify location more precisely (e.g. "Cambridge, MA" instead of "Cambridge").
- **Venue discovery yields fewer than 5 venues** → tell the user the city's coverage looks thin and ask if they want to provide a few venue URLs manually (which you'd add to the cache file directly).
- **Calendar pages mostly bot-block** → the scraper uses a real browser User-Agent; if many venues still come back `failed` with `http 403`, tell the user honestly and offer to retry tomorrow. If `FIRECRAWL_API_KEY` is set in the environment the scraper automatically retries unrecognized pages through the Firecrawl API; without it those venues land in `needs_llm`.
- **A venue reports `DNS lookup failed` / `http 404`** → its URL in the venue list is stale. Say so, skip it, and suggest `refresh venues for <city>` (or fix the entry in the venue file directly).
- **Scraper says `recognized <platform> markup but parsed 0 events`** → the venue redesigned its site; treat as `failed` and use the time-boxed fallback.
- **No matches at all** → mention how many venues had events in the window vs. how many matched their taste; suggest widening dates or refreshing taste.
