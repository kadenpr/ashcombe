"""
linkedin_fetcher.py — LinkedIn company posts fetcher via Apify.

Uses the datadoping/linkedin-company-posts-scraper actor.
Companies without a linkedin_url in companies.csv are silently skipped.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fetcher import NewsItem
from utils import normalise_url, parse_posted_ago

logger = logging.getLogger(__name__)

ACTOR_ID = "datadoping/linkedin-company-posts-scraper"
MAX_POSTS_PER_COMPANY = 5
ACTOR_TIMEOUT_SECS = 600



def fetch_linkedin_posts(
    companies: list[dict],
    since: datetime,
    seen_hashes: set[str],
) -> dict[str, list[NewsItem]]:
    """
    Fetch LinkedIn posts for all companies that have a linkedin_url.
    Returns a dict keyed by company name (same shape as fetcher.fetch_all).
    Gracefully returns {} if APIFY_API_TOKEN is not set or apify-client is missing.
    """
    try:
        from apify_client import ApifyClient
    except ImportError:
        logger.error("apify-client not installed — run: pip install apify-client")
        return {}

    api_token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not api_token:
        logger.warning("APIFY_API_TOKEN not set — skipping LinkedIn fetch")
        return {}

    # Build normalised URL -> company name map
    url_to_company: dict[str, str] = {}
    company_urls: list[str] = []
    for company in companies:
        linkedin_url = company.get("linkedin_url", "").strip()
        if not linkedin_url:
            continue
        url_to_company[normalise_url(linkedin_url)] = company["name"]
        company_urls.append(linkedin_url)

    if not company_urls:
        logger.info("No linkedin_url entries in companies.csv — skipping LinkedIn fetch")
        return {}

    logger.info(
        "Fetching LinkedIn posts for %d companies via Apify (%s)...",
        len(company_urls),
        ACTOR_ID,
    )

    client = ApifyClient(api_token)
    run_input = {
        "companies": company_urls,
        "maxResults": MAX_POSTS_PER_COMPANY,
    }

    try:
        run = client.actor(ACTOR_ID).call(
            run_input=run_input,
            timeout_secs=ACTOR_TIMEOUT_SECS,
        )
    except Exception as exc:
        logger.error("Apify actor run failed: %s", exc)
        return {}

    now = datetime.now(timezone.utc)
    results: dict[str, list[NewsItem]] = {}

    for raw in client.dataset(run["defaultDatasetId"]).iterate_items():
        # Map back to company using the input URL field
        input_url = raw.get("input", "")
        company_name = url_to_company.get(normalise_url(input_url))
        if not company_name:
            logger.debug("Could not map LinkedIn post to company: %s", input_url)
            continue

        # Parse relative date
        published = parse_posted_ago(raw.get("postedAgo", ""), now)
        if published is None:
            logger.debug("LinkedIn post has no parseable date — skipping")
            continue

        if published <= since:
            continue

        # Build post URL from activity URN
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

        title = text[:120].replace("\n", " ")
        if len(text) > 120:
            title += "…"

        item = NewsItem(
            company=company_name,
            title=title,
            url=post_url,
            source="LinkedIn",
            published=published,
        )

        if item.item_hash in seen_hashes:
            continue

        results.setdefault(company_name, []).append(item)

    # Sort newest-first, cap per company
    for name in results:
        results[name].sort(key=lambda x: x.published, reverse=True)
        results[name] = results[name][:MAX_POSTS_PER_COMPANY]
        logger.info("  LinkedIn %s: %d post(s)", name, len(results[name]))

    total = sum(len(v) for v in results.values())
    logger.info(
        "LinkedIn fetch complete: %d post(s) across %d companies",
        total,
        len(results),
    )
    return results
