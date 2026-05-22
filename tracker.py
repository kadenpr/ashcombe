"""
tracker.py — Main entry point for Ashcombe AI News Tracker.

Run directly:
    python tracker.py            # normal run
    python tracker.py --dry-run  # print digest to console, no email sent

Schedule (cron example — 07:00 UK time):
    0 7 * * * cd /path/to/Ashcombe && /path/to/venv/bin/python tracker.py
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from fetcher import fetch_all, NewsItem
from linkedin_fetcher import fetch_linkedin_posts
from about_fetcher import fetch_profile_changes
from jobs_fetcher import fetch_jobs_changes
from people_fetcher import fetch_people_posts
from summariser import Summariser
from mailer import send_digest, send_failure_alert

UK_TZ = ZoneInfo("Europe/London")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STATE_FILE = Path("state.json")
COMPANIES_FILE = Path("companies.csv")
DEFAULT_LOOKBACK_HOURS = 24  # used on very first run only
MAX_TIER1_PER_COMPANY = 2  # excess tier 1 items overflow to Also in Brief

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("state.json is corrupt — resetting to empty state")
    return {"last_run": None, "seen_hashes": [], "company_profiles": {}}


def save_state(
    last_run: datetime,
    seen_hashes: set[str],
    company_profiles: dict | None = None,
    company_jobs: dict | None = None,
) -> None:
    data = {
        "last_run": last_run.isoformat(),
        # Keep the last 10 000 hashes to cap file size
        "seen_hashes": list(seen_hashes)[-10_000:],
        "company_profiles": company_profiles or {},
        "company_jobs": company_jobs or {},
    }
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("State saved (last_run=%s, %d hashes)", last_run.isoformat(), len(seen_hashes))


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_companies() -> list[dict]:
    if not COMPANIES_FILE.exists():
        logger.error("companies.csv not found")
        sys.exit(1)
    with open(COMPANIES_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> None:
    run_dt = datetime.now(timezone.utc)
    logger.info("=== Ashcombe AI News Tracker — %s ===", run_dt.isoformat())

    # 1. Load state
    state = load_state()
    seen_hashes: set[str] = set(state.get("seen_hashes", []))
    company_profiles: dict = state.get("company_profiles", {})
    company_jobs: dict = state.get("company_jobs", {})
    last_run_raw: str | None = state.get("last_run")
    lookback_hours = int(os.environ.get("LOOKBACK_HOURS", DEFAULT_LOOKBACK_HOURS))

    if last_run_raw:
        since = datetime.fromisoformat(last_run_raw).replace(tzinfo=timezone.utc)
        logger.info("Last run: %s", since.isoformat())
    else:
        since = run_dt - timedelta(hours=lookback_hours)
        logger.info("No previous run — using %dh lookback (%s)", lookback_hours, since.isoformat())

    # 2. Load companies
    companies = load_companies()
    company_search_names = {c["name"]: c.get("search_name", "").strip() for c in companies}
    logger.info("Monitoring %d companies", len(companies))

    # 3. Fetch RSS feeds
    logger.info("--- Fetching news feeds ---")
    raw_results: dict[str, list[NewsItem]] = fetch_all(companies, since, seen_hashes)
    rss_total = sum(len(v) for v in raw_results.values())
    logger.info("Fetched %d new item(s) from RSS", rss_total)

    # 3b. Fetch LinkedIn company posts (skipped if APIFY_API_TOKEN not set)
    logger.info("--- Fetching LinkedIn company posts ---")
    linkedin_results = fetch_linkedin_posts(companies, since, seen_hashes)
    for company, items in linkedin_results.items():
        raw_results.setdefault(company, []).extend(items)

    # 3c. Fetch senior employee LinkedIn posts
    logger.info("--- Fetching senior employee posts ---")
    people_results = fetch_people_posts(since, seen_hashes)
    for company, items in people_results.items():
        raw_results.setdefault(company, []).extend(items)

    total_fetched = sum(len(v) for v in raw_results.values())
    logger.info("Fetched %d new item(s) total (RSS + LinkedIn + people)", total_fetched)

    # 3d. Check LinkedIn company profiles — Mondays only (weekly cadence)
    is_monday = run_dt.weekday() == 0 or os.environ.get("FORCE_MONDAY", "").lower() in ("1", "true", "yes")
    if is_monday:
        logger.info("--- Checking LinkedIn company profiles (Monday weekly check) ---")
        profile_changes, updated_profiles = fetch_profile_changes(companies, company_profiles)
        logger.info("%d profile change(s) detected", len(profile_changes))
    else:
        logger.info("Skipping profile check (runs Mondays only)")
        profile_changes, updated_profiles = [], company_profiles

    # 3e. Check LinkedIn job postings — Mondays only (weekly cadence)
    if is_monday:
        logger.info("--- Checking LinkedIn job postings (Monday weekly check) ---")
        jobs_changes, updated_jobs = fetch_jobs_changes(companies, company_jobs)
        logger.info("%d jobs change(s) detected", len(jobs_changes))
    else:
        logger.info("Skipping jobs check (runs Mondays only)")
        jobs_changes, updated_jobs = [], company_jobs

    if total_fetched == 0 and not profile_changes and not jobs_changes:
        logger.info("No new items, profile changes, or jobs changes — exiting cleanly (no email sent)")
        save_state(run_dt, seen_hashes, updated_profiles, updated_jobs)
        sys.exit(0)

    # 4. Classify + evaluate with Anthropic
    logger.info("--- Classifying items with Anthropic ---")
    summariser = Summariser()

    # 4a. Classify news items
    flat_items: list[NewsItem] = [
        item for items in raw_results.values() for item in items
    ]
    new_hashes: set[str] = {item.item_hash for item in flat_items}

    digest: dict[str, list[dict]] = {}
    secondary_digest: dict[str, list[dict]] = {}
    if flat_items:
        item_dicts = [
            {"company": item.company, "title": item.title, "source": item.source, "url": item.url,
             "search_name": company_search_names.get(item.company, "")}
            for item in flat_items
        ]
        classified = summariser.classify_batch(item_dicts)

        for news_item, (item_dict, result) in zip(flat_items, classified):
            company = news_item.company
            entry = {
                "summary": result.summary,
                "url": news_item.url,
                "source": news_item.source,
                "published": news_item.published.strftime("%-d %b %Y"),
                "category": result.category,
            }
            if result.relevant and len(digest.get(company, [])) < MAX_TIER1_PER_COMPANY:
                digest.setdefault(company, []).append(entry)
            elif result.summary:
                secondary_digest.setdefault(company, []).append(entry)

    total_relevant = sum(len(v) for v in digest.values())
    total_secondary = sum(len(v) for v in secondary_digest.values())
    logger.info("%d secondary item(s) across %d company/companies", total_secondary, len(secondary_digest))
    logger.info(
        "%d relevant item(s) across %d company/companies",
        total_relevant,
        len(digest),
    )

    # 4b. Evaluate profile changes
    if profile_changes:
        logger.info("--- Evaluating profile changes ---")
        for change in profile_changes:
            change.evaluation = summariser.evaluate_profile_change(
                change.company, change.field, change.old_value, change.new_value
            )

    # 4c. Evaluate jobs changes
    if jobs_changes:
        logger.info("--- Evaluating jobs changes ---")
        for change in jobs_changes:
            change.evaluation = summariser.evaluate_jobs_change(
                company=change.company,
                current_total=change.current.total,
                previous_total=change.previous.total,
                new_senior_roles=change.new_senior_roles,
                top_functions=change.current.functions,
            )

    # 5. Exit cleanly if nothing to report
    if total_relevant == 0 and total_secondary == 0 and not profile_changes and not jobs_changes:
        logger.info("No relevant items after filtering — exiting cleanly (no email sent)")
        seen_hashes.update(new_hashes)
        save_state(run_dt, seen_hashes, updated_profiles, updated_jobs)
        sys.exit(0)

    # 6. Render and send digest
    company_owners = {c["name"]: c.get("owner", "") for c in companies}
    logger.info("--- Sending digest ---")
    send_digest(
        digest,
        secondary_digest=secondary_digest,
        profile_changes=profile_changes,
        jobs_changes=jobs_changes,
        company_owners=company_owners,
        run_dt=run_dt,
        dry_run=dry_run,
    )

    # 7. Persist state — update seen hashes AFTER successful send
    seen_hashes.update(new_hashes)
    save_state(run_dt, seen_hashes, updated_profiles, updated_jobs)

    logger.info(
        "=== Done — %d relevant items, %d profile change(s), %d jobs change(s) sent ===",
        total_relevant,
        len(profile_changes),
        len(jobs_changes),
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ashcombe AI News Tracker — fetch, summarise, and email company news."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
        help="Render digest to console; do not send email or update state.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    load_dotenv()
    try:
        run(dry_run=args.dry_run)
    except Exception as exc:
        run_dt = datetime.now(timezone.utc)
        logger.critical("Unhandled exception: %s", exc, exc_info=True)
        if not args.dry_run:
            send_failure_alert(exc, run_dt)
        sys.exit(1)


if __name__ == "__main__":
    main()
