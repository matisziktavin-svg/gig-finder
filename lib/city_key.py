"""Normalize a freeform location string into a cache-friendly slug.

Used to key per-city venue lists at ~/.gigfinder/venues/<city_key>.json.
"""
from __future__ import annotations

import re
import unicodedata


def city_key(location: str) -> str:
    """
    "Boston, MA"          -> "boston-ma"
    "New York City"       -> "new-york-city"
    "London, UK"          -> "london-uk"
    "Saint-Étienne"       -> "saint-etienne"
    """
    if not location:
        raise ValueError("location must be non-empty")
    # Strip diacritics
    normalized = unicodedata.normalize("NFKD", location)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    # Lowercase, replace non-alphanumerics with hyphen
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower())
    # Collapse hyphens, strip ends
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        raise ValueError(f"Could not derive slug from: {location!r}")
    return slug


# ── smoke test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cases = [
        "Boston, MA",
        "New York City",
        "London, UK",
        "Los Angeles, CA, USA",
        "Saint-Étienne",
        "São Paulo, Brazil",
        "  Berlin   ",
    ]
    for c in cases:
        print(f"  {c!r:>32}  ->  {city_key(c)!r}")
