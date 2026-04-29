# gig-finder

A Claude Code skill that finds concerts and gigs near a given location and date range, matched against your real listening history (Last.fm + Spotify) and expanded via similar-artist lookups (Last.fm, MusicBrainz). Event data comes from Bandsintown and Ticketmaster.

## Status

Under active development. See [the implementation plan](https://github.com/matisziktavin-svg/gig-finder) for current phase. Setup walkthrough below will land in Phase 9.

## Requirements

- Spotify Premium account (required by Feb 2026 dev-mode rule)
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- A Last.fm account with [scrobbling](https://www.last.fm/about/trackmymusic) enabled (the more history, the better the recommendations)

## Setup

_Coming in Phase 9._

## Usage

Once installed, in Claude Code:

```
> refresh my taste profile
> find me gigs in Boston from June 1 to June 15
```

## How it works

_Coming in Phase 9._

## License

MIT
