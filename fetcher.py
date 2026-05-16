"""
fetcher.py — Google News RSS fetcher for Ashcombe AI News Tracker.

Queries Google News RSS for each company and returns items published
since the given cutoff datetime. Uses feedparser; no API key required.
"""

from __future__ import annotations

import hashlib
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import ssl

import certifi
import feedparser

logger = logging.getLogger(__name__)

# Ensure feedparser uses the certifi CA bundle on macOS / systems with no
# system CA store configured for Python.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _feedparser_parse(url: str) -> feedparser.FeedParserDict:
    """Parse an RSS URL using a certifi-backed SSL context."""
    import urllib.request
    handler = urllib.request.HTTPSHandler(context=_SSL_CONTEXT)
    opener = urllib.request.build_opener(handler)
    with opener.open(url, timeout=20) as resp:
        raw = resp.read()
    return feedparser.parse(raw)

GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q={query}&hl=en-GB&gl=GB&ceid=GB:en"
)

# Polite delay between RSS requests (seconds)
REQUEST_DELAY = 1.0

# Cap per company to keep Claude costs down
MAX_ITEMS_PER_COMPANY = 5


@dataclass
class NewsItem:
    company: str
    title: str
    url: str
    source: str
    published: datetime
    item_hash: str = field(init=False)

    def __post_init__(self) -> None:
        # Hash on URL so re-runs are idempotent regardless of title edits
        self.item_hash = hashlib.sha256(self.url.encode()).hexdigest()


def _parse_published(entry: feedparser.FeedParserDict) -> Optional[datetime]:
    """Return a timezone-aware datetime from an RSS entry, or None."""
    # feedparser exposes published_parsed as a time.struct_time in UTC
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    # Fall back to raw string
    raw = getattr(entry, "published", None)
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc)
        except Exception:
            pass
    return None


def fetch_company_news(
    company_name: str,
    since: datetime,
    seen_hashes: set[str],
) -> list[NewsItem]:
    """
    Fetch Google News RSS for *company_name* and return items that:
      - were published after *since*
      - have not been seen before (not in *seen_hashes*)
    """
    query = urllib.parse.quote_plus(company_name)
    url = GOOGLE_NEWS_RSS.format(query=query)
    logger.debug("Fetching RSS for %s: %s", company_name, url)

    try:
        feed = _feedparser_parse(url)
    except Exception as exc:
        logger.warning("Network error fetching %s: %s", company_name, exc)
        return []

    if feed.bozo and not feed.entries:
        logger.warning("RSS parse error for %s: %s", company_name, feed.bozo_exception)
        return []

    items: list[NewsItem] = []
    for entry in feed.entries:
        published = _parse_published(entry)
        if published is None:
            logger.debug("Skipping entry with no publish date: %s", entry.get("title"))
            continue

        if published <= since:
            continue

        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        source = entry.get("source", {}).get("title", "Unknown")

        if not title or not link:
            continue

        item = NewsItem(
            company=company_name,
            title=title,
            url=link,
            source=source,
            published=published,
        )

        if item.item_hash in seen_hashes:
            logger.debug("Skipping already-seen item: %s", title)
            continue

        items.append(item)

    # Newest first, then cap to avoid sending stale bulk to Claude
    items.sort(key=lambda x: x.published, reverse=True)
    items = items[:MAX_ITEMS_PER_COMPANY]

    logger.info("  %s: %d new item(s) since %s", company_name, len(items), since.isoformat())
    return items


def fetch_all(
    companies: list[dict],
    since: datetime,
    seen_hashes: set[str],
) -> dict[str, list[NewsItem]]:
    """
    Fetch news for all companies. Returns a dict keyed by company name.
    *companies* is a list of dicts with at least a 'name' key.
    """
    results: dict[str, list[NewsItem]] = {}
    for i, company in enumerate(companies):
        name = company["name"]
        items = fetch_company_news(name, since, seen_hashes)
        results[name] = items
        if i < len(companies) - 1:
            time.sleep(REQUEST_DELAY)
    return results


# ---------------------------------------------------------------------------
# Dry-run smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import csv
    import json
    import sys
    from pathlib import Path

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    state_path = Path("state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    seen = set(state.get("seen_hashes", []))

    # Default to last 24 h if no state
    from datetime import timedelta
    last_run_raw = state.get("last_run")
    since = (
        datetime.fromisoformat(last_run_raw).replace(tzinfo=timezone.utc)
        if last_run_raw
        else datetime.now(timezone.utc) - timedelta(hours=24)
    )

    companies: list[dict] = []
    with open("companies.csv", newline="") as f:
        companies = list(csv.DictReader(f))

    # Limit to first 3 for a quick smoke test
    results = fetch_all(companies[:3], since, seen)

    total = sum(len(v) for v in results.values())
    print(f"\n=== Fetcher dry-run: {total} new item(s) across {len(results)} companies ===")
    for company, items in results.items():
        print(f"\n{company} ({len(items)} items):")
        for item in items:
            print(f"  [{item.published.strftime('%Y-%m-%d %H:%M')}] {item.title}")
            print(f"    {item.url}")

    sys.exit(0)
