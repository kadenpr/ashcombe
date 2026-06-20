"""
utils.py — Shared helpers for Ashcombe AI News Tracker.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional


def normalise_url(url: str) -> str:
    return url.rstrip("/").lower()


def parse_posted_ago(posted_ago: str, now: datetime) -> Optional[datetime]:
    """
    Convert LinkedIn's relative 'postedAgo' string to an approximate datetime.
    Examples: '3d', '2w', '5h', '1mo', '2yr'
    Uses the most recent possible interpretation so recent posts aren't filtered out.
    """
    if not posted_ago:
        return None
    match = re.match(r"(\d+)\s*(h|d|w|mo|yr|y)", posted_ago.strip().lower())
    if not match:
        return None
    n, unit = int(match.group(1)), match.group(2)
    if unit == "h":
        return now - timedelta(hours=n)
    if unit == "d":
        return now - timedelta(hours=max(1, n * 24 - 12))
    if unit == "w":
        return now - timedelta(weeks=n)
    if unit == "mo":
        return now - timedelta(days=n * 30)
    if unit in ("yr", "y"):
        return now - timedelta(days=n * 365)
    return None


def strip_json_fences(text: str) -> str:
    """Remove markdown code fences that models sometimes wrap JSON in."""
    return re.sub(r"```(?:json)?", "", text).strip()
