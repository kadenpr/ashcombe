"""
people_fetcher.py — Senior employee LinkedIn posts fetcher for Ashcombe AI News Tracker.

Reads people.csv (name, title, company, linkedin_url) and fetches recent posts
from each person's LinkedIn profile via Apify. Returns NewsItem objects that flow
into the standard classification pipeline — they appear in the news section of
the digest with "{name} ({title})" as the source.

To use: populate people.csv with the senior employees you want to track.
The 'company' column must match a name in companies.csv exactly.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fetcher import NewsItem
from utils import normalise_url, parse_posted_ago

logger = logging.getLogger(__name__)

ACTOR_ID = "datadoping/linkedin-profile-posts-scraper"
MAX_POSTS_PER_PERSON = 3
ACTOR_TIMEOUT_SECS = 600
PEOPLE_FILE = Path("people.csv")



def _load_people() -> list[dict]:
    """Return rows from people.csv, silently returning [] if missing or empty."""
    if not PEOPLE_FILE.exists():
        logger.info("people.csv not found — skipping senior employee posts")
        return []
    with open(PEOPLE_FILE, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("linkedin_url", "").strip()]
    if not rows:
        logger.info("people.csv is empty — add employees to track their posts")
    return rows


def fetch_people_posts(
    since: datetime,
    seen_hashes: set[str],
) -> dict[str, list[NewsItem]]:
    """
    Fetch recent LinkedIn posts from all people listed in people.csv.
    Returns a dict keyed by company name (same shape as fetcher.fetch_all).
    Gracefully returns {} if APIFY_API_TOKEN is missing, people.csv is empty,
    or the actor call fails.
    """
    people = _load_people()
    if not people:
        return {}

    try:
        from apify_client import ApifyClient
    except ImportError:
        logger.error("apify-client not installed — skipping people posts")
        return {}

    api_token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not api_token:
        logger.warning("APIFY_API_TOKEN not set — skipping people posts")
        return {}

    # Build URL → person mapping
    url_to_person: dict[str, dict] = {
        normalise_url(p["linkedin_url"]): p
        for p in people
    }
    profile_urls = [p["linkedin_url"].strip() for p in people]

    logger.info(
        "Fetching LinkedIn posts for %d people via Apify (%s)...",
        len(profile_urls),
        ACTOR_ID,
    )

    client = ApifyClient(api_token)
    run_input = {
        "profileUrls": profile_urls,
        "maxResults": MAX_POSTS_PER_PERSON,
    }

    try:
        run = client.actor(ACTOR_ID).call(
            run_input=run_input,
            timeout_secs=ACTOR_TIMEOUT_SECS,
        )
    except Exception as exc:
        logger.error("Apify people-posts actor run failed: %s", exc)
        return {}

    now = datetime.now(timezone.utc)
    results: dict[str, list[NewsItem]] = {}

    for raw in client.dataset(run["defaultDatasetId"]).iterate_items():
        # Resolve which person this post belongs to via the input URL
        input_url = raw.get("input") or raw.get("profileUrl") or raw.get("profile_url") or ""
        person = url_to_person.get(normalise_url(input_url))
        if not person:
            logger.debug("Could not map post to a person: %s", input_url)
            continue

        published = parse_posted_ago(raw.get("postedAgo", ""), now)
        if published is None:
            logger.debug("Post from %s has no parseable date — skipping", person["name"])
            continue

        if published <= since:
            continue

        activity_urn = raw.get("activity_urn", "")
        post_url = (
            f"https://www.linkedin.com/feed/update/{activity_urn}/"
            if activity_urn
            else ""
        )
        if not post_url:
            continue

        text = (raw.get("text") or "").strip()
        if not text:
            continue

        # Truncate long post text to a readable headline
        title = text[:120].replace("\n", " ")
        if len(text) > 120:
            title += "…"

        # Source line identifies the person clearly
        source = f"{person['name']} ({person['title']}) · LinkedIn"

        item = NewsItem(
            company=person["company"],
            title=title,
            url=post_url,
            source=source,
            published=published,
        )

        if item.item_hash in seen_hashes:
            continue

        results.setdefault(person["company"], []).append(item)

    # Sort newest-first, cap per person (not per company — already capped by actor)
    for company in results:
        results[company].sort(key=lambda x: x.published, reverse=True)
        logger.info("  People posts — %s: %d post(s)", company, len(results[company]))

    total = sum(len(v) for v in results.values())
    logger.info("People posts fetch complete: %d post(s) across %d companies", total, len(results))
    return results
