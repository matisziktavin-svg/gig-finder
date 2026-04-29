---
name: gig-finder
description: Find indie / small-venue concerts near a location within a date range, matched against the user's real listening history (Last.fm + Spotify) and expanded with similar artists. Triggers on requests like "find me gigs / shows / concerts in <place> from <date> to <date>", "what's playing in <city> next month", "refresh my taste profile", or "refresh venues for <city>".
---

# gig-finder

A skill that combines deterministic Python helpers with your own web research to surface concerts the user will care about. The skill is **indie-only by design** — large venues (stadiums, arenas, amphitheaters, theaters > ~2000 cap) are explicitly out of scope.

All commands assume the current working directory is the skill directory. Use `uv run python ...` to invoke scripts so they pick up the project's virtualenv.

---

## Mode dispatch

Inspect the user's request and pick exactly one mode:

| Phrase pattern | Mode |
|---|---|
| "find / list me gigs / shows / concerts in <place>" + date range | `find-gigs` |
| "what's playing in <place> [during X]" | `find-gigs` |
| "refresh my taste profile" / "rebuild taste" / "update my music tastes" | `refresh-taste` |
| "refresh venues for <city>" / "rediscover venues in <city>" | `refresh-venues` |

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

6. Save the list with the Write tool to `venue_cache_path` (a JSON object with `discovered_at`, `city_key`, `venues`):
   ```json
   {
     "discovered_at": "<ISO timestamp now>",
     "city_key": "<from prepare_search>",
     "venues": [...]
   }
   ```

### Step 4 — fetch each venue's calendar (cache or extract)

For each venue (cap at 20), in order:

1. Determine the calendar URL (`events_url` if present, else `url`).

2. Check the per-venue calendar cache:
   ```bash
   uv run python scripts/cache_io.py status calendar --url "<events_url>" --ttl-days 1
   ```
   This returns `{path, exists, age_days, stale}`. If `stale: false` → use the cached file (Read it, parse the JSON `events` array).

3. If stale, **WebFetch** the calendar page with a prompt that asks to extract upcoming events as JSON. Tell the model:
   - Look first for structured data: `<script type="application/ld+json">` with `@type: MusicEvent` / `Event`, or linked iCal / RSS feeds in `<link rel>`.
   - Otherwise, parse the visible event list / calendar.
   - Return a JSON array of:
     ```json
     {
       "date": "YYYY-MM-DD",         // required
       "time": "HH:MM",              // optional, 24h
       "artists": ["Headliner", ...],// required, in lineup order
       "venue": "<venue name>",      // required (use the venue name from your list)
       "venue_url": "<events_url>",  // for traceability
       "ticket_url": "<deep link>"   // if a per-event ticket URL exists
     }
     ```
   - Restrict to events with `date` between `date_from` and `date_to` inclusive.
   - Skip ticket-only listings without a date or an artist.

4. Save the result (even if empty) to the cache path returned in step 2:
   ```json
   {
     "fetched_at": "<ISO now>",
     "venue": "<name>",
     "events_url": "...",
     "events": [...]
   }
   ```

If the venue page is bot-blocked / 403 / Cloudflare-challenged: write an empty events array to the cache (so we don't retry within 24h) and move on.

### Step 5 — aggregate and filter

Concatenate the `events` arrays across all venues into a single JSON list. Write it to a temp file, then pipe to `finalize_search.py`:

```bash
# Write the aggregated array (use the Write tool) to /tmp/gigfinder_events.json,
# then:
cat /tmp/gigfinder_events.json | uv run python scripts/finalize_search.py \
  --from <date_from> --to <date_to>
```

(Don't try to inline large JSON in `echo` / heredoc — use the Write tool to a temp file and `cat` it in.)

The output JSON has `stats` and `events` (filtered, tagged, sorted). The `events` array is what to render.

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
- If `events` is empty, say so plainly and suggest either widening the date range, refreshing venues, or refreshing the taste profile.

End with a one-line summary of `stats` (e.g. "Scanned 18 venues → 47 events in window → 12 matched your taste").

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
| Per-venue calendar | 24 hours | `~/.gigfinder/calendars/<sha256>.json` |
| Geocode | forever | `~/.gigfinder/geocode.cache.json` |
| Last.fm similar artists | 30 days | `~/.gigfinder/similar.json` |

The user can manually delete any of these to force a refresh. `refresh-venues` and `refresh-taste` are the supported commands.

---

## Failure modes

- **No taste profile** → prompt user to run `refresh-taste` first.
- **Geocoding returns nothing** → ask the user to specify location more precisely (e.g. "Cambridge, MA" instead of "Cambridge").
- **Venue discovery yields fewer than 5 venues** → tell the user the city's coverage looks thin and ask if they want to provide a few venue URLs manually (which you'd add to the cache file directly).
- **Calendar pages mostly bot-block** → tell the user honestly; offer to retry tomorrow (the 24h cache TTL prevents thrashing).
- **No matches at all** → mention how many venues had events in the window vs. how many matched their taste; suggest widening dates or refreshing taste.
