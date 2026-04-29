# gig-finder

A Claude Code skill that finds **indie / small-venue concerts** near a location and date range, matched against your real listening history (Last.fm + Spotify) and expanded with similar artists. Large mainstream venues (stadiums, arenas, amphitheaters) are intentionally out of scope.

## What it does

```
> find me gigs in Boston June 1 to June 15

Sat 2026-06-05 · 8pm — The Strokes @ The Sinclair, Cambridge
  [Top favorite] · [Genre: indie rock]
  Tickets: https://...

Sun 2026-06-08 · 9pm — Sports Team @ Brighton Music Hall, Allston
  [Top favorite] · [Genre: post-punk]
  Tickets: https://...

…

Scanned 18 venues → 47 events in window → 12 matched your taste
```

Behind the scenes: Claude does the venue discovery (web-searching for the city's indie venues) and per-venue calendar extraction. Python builds the taste profile, geocodes, expands the artist universe via Last.fm `artist.getSimilar`, and ranks the matches.

## Why this works the way it does

The free indie concert APIs are essentially gone in 2026:

- **Bandsintown** — public API now returns AWS IAM "explicit deny".
- **Eventbrite** — public event search killed Feb 2020.
- **Songkick** — closed to new public devs since ~2019.

Ticketmaster's Discovery API still works but skews mainstream. So instead of relying on dead APIs, this skill uses **Claude itself** as the discovery engine: it searches for venues, fetches each venue's calendar page, and extracts events. Caching makes repeated searches in the same city fast and cheap.

## Requirements

- **Spotify Premium account** (Feb 2026 dev-mode rule for Web API access)
- **Python 3.12+**
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- A **Last.fm account** with [scrobbling enabled](https://www.last.fm/about/trackmymusic) — the more history, the better the recommendations. (You'll get *some* signal even with little history thanks to the Spotify-side data.)
- **Claude Code** (CLI) or any Claude client that supports skills

## Setup

### 1. Clone both repos

```bash
git clone https://github.com/matisziktavin-svg/gig-finder.git
git clone https://github.com/matisziktavin-svg/spotify-mcp.git
```

The second one is a **fork** of [verIdyia/spotify-mcp](https://github.com/verIdyia/spotify-mcp) that adds the `user-follow-read` scope and a `spotify_followed_artists` tool. You need the fork — upstream won't expose your followed artists.

### 2. Install dependencies

```bash
cd gig-finder && uv sync
cd ../spotify-mcp && uv sync
```

Don't have `uv`? On Windows: `winget install --id=astral-sh.uv -e` or `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`. macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh | sh`.

### 3. Create a Spotify Developer App

1. Go to <https://developer.spotify.com/dashboard> and **Create app**.
2. Name and description: anything.
3. **Redirect URI**: `http://127.0.0.1:8080/callback` (or pick another free port — must match `SPOTIFY_REDIRECT_URI` in `.env` below).
4. APIs used: tick **Web API**.
5. Save. Open the app → Settings → note the **Client ID** and view the **Client Secret**.

### 4. Get a Last.fm API key

1. Go to <https://www.last.fm/api/account/create>.
2. Fill in any name + description; leave callback / homepage blank.
3. Submit. Copy the **API Key** (ignore the shared secret).
4. Note your Last.fm **username** (the one that's been scrobbling).

### 5. Fill in `.env`

In the `gig-finder` dir:

```bash
cp .env.example .env
```

Then edit `.env`:

```
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8080/callback
LASTFM_API_KEY=...
LASTFM_USERNAME=...
```

### 6. Authorize Spotify (one-time)

From the `spotify-mcp` dir:

```bash
SPOTIFY_CLIENT_ID=... SPOTIFY_CLIENT_SECRET=... uv run spotify-mcp --auth
```

A browser opens. Log in to Spotify and click **Agree**. The consent screen should mention "**Access your followed artists**" — that's the new scope from the fork. The token is saved to `~/.spotify_mcp_cache.json`. The skill's Python reads from that file directly.

### 7. (Optional) Add the MCP server to your Claude config

This is only needed if you want Claude to control Spotify directly (search, playback, playlists) — the gig-finder skill doesn't require it. If you want it, add to `~/.config/claude/claude_desktop_config.json` (or your IDE's MCP config):

```json
{
  "mcpServers": {
    "spotify": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/spotify-mcp", "run", "spotify-mcp"],
      "env": {
        "SPOTIFY_CLIENT_ID": "...",
        "SPOTIFY_CLIENT_SECRET": "...",
        "SPOTIFY_REDIRECT_URI": "http://127.0.0.1:8080/callback"
      }
    }
  }
}
```

### 8. Install as a Claude skill

Claude Code looks for skills under `~/.claude/skills/<name>/`. Either:

**(a) Clone directly into the skills directory:**
```bash
git clone https://github.com/matisziktavin-svg/gig-finder.git ~/.claude/skills/gig-finder
```

**(b) Or symlink/junction your dev clone:**
```bash
# macOS/Linux
ln -s /path/to/gig-finder ~/.claude/skills/gig-finder

# Windows (no admin needed — uses junction):
mklink /J %USERPROFILE%\.claude\skills\gig-finder C:\path\to\gig-finder
```

If you used (b), you can keep developing in your original clone and changes are live.

### 9. Build your taste profile

In Claude Code:

```
> refresh my taste profile
```

This runs `scripts/refresh_taste.py`, which mines your Last.fm + Spotify data into `~/.gigfinder/taste.json`. Takes about a minute.

### 10. Find some gigs

```
> find me gigs in Boston from June 1 to June 15
```

The first search in a new city triggers venue discovery (Claude web-searches for indie venues, ~30 sec) and then per-venue calendar fetches (~1-2 min for 20 venues). Subsequent searches in the same city use the 365-day venue cache and 24-hour calendar cache — they finish in seconds.

## Usage examples

```
> find me indie shows in Brooklyn this weekend
> what's playing in Berlin between July 1 and July 10
> refresh my taste profile
> refresh venues for Boston   (forces a fresh venue discovery)
```

## How it works (one paragraph)

`refresh-taste` aggregates Last.fm top artists, top tags, loved tracks, and recent plays with Spotify likes, follows, and owned playlists into a weighted artist score. `find-gigs` runs a small Python prelude (`prepare_search.py`) to validate inputs and geocode, then hands off to Claude to find ~20 indie venues for the city (cached 365 days), fetch each venue's calendar (cached 24h), and extract events. The events are piped into `finalize_search.py`, which builds the artist universe (taste profile + Last.fm `artist.getSimilar` for the top ~30 artists), filters events to those with matching artists, tags each with the strongest signal (`Top favorite` / `Loved` / `Followed` / `Similar to X`), and sorts by date.

## Limitations (honest)

- **Coverage depends on Claude's web search quality.** Cities with rich editorial coverage (NYC, LA, London, Berlin) will be well-discovered. Smaller scenes may be sparse.
- **Bot-blocked venues won't be readable.** Some venue sites are behind Cloudflare or aggressive anti-scraping. Those return zero events.
- **DIY-only shows are out of reach.** Anything that lives only on Instagram, posters, mailing lists, or word of mouth — no tool can find these.
- **First search in a new city is slow** (~1-2 min). Subsequent searches are seconds thanks to caching.
- **Spotify side is genre-blind** since the Feb 2026 API removed genres from artist objects. We use Last.fm tags as the genre source, which is actually richer.

## Troubleshooting

- **"No taste profile found"** — run `refresh my taste profile` first.
- **`spotify-mcp --auth` browser shows "INVALID_REDIRECT_URI"** — the URI in your dev app dashboard doesn't match `SPOTIFY_REDIRECT_URI` in `.env`. Make them identical.
- **`spotify_followed_artists` not in your auth scopes** — you installed upstream `verIdyia/spotify-mcp` instead of the fork, or didn't re-run `--auth` after switching. Re-run from the `matisziktavin-svg/spotify-mcp` clone.
- **A venue's calendar returns no events** — could be bot-blocked (try opening the URL in a browser to verify). Cache will retry in 24h.
- **Few or no matches** — your taste profile may be sparse; try `refresh my taste profile` again, then widen the date range.
- **Want to clear caches?** Delete `~/.gigfinder/` (or specific files within: `taste.json`, `venues/<city>.json`, `calendars/`, `similar.json`).

## Project layout

```
gig-finder/
├── SKILL.md           # orchestration prompt Claude follows
├── scripts/
│   ├── refresh_taste.py    # mines Last.fm + Spotify
│   ├── prepare_search.py   # validates input, geocodes
│   ├── finalize_search.py  # filters/tags/sorts Claude-extracted events
│   └── cache_io.py         # cache path/freshness helper for SKILL.md
└── lib/
    ├── lastfm.py / spotify.py / musicbrainz.py / geocode.py
    ├── city_key.py / cache.py / env.py / ranking.py
```

## Credits

- **MCP fork** of [verIdyia/spotify-mcp](https://github.com/verIdyia/spotify-mcp) (which itself is a fork of [varunneal/spotify-mcp](https://github.com/varunneal/spotify-mcp)).
- **Built with** [Claude Code](https://claude.com/claude-code).

## License

MIT
