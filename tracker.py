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
from summariser import Summariser, SummaryResult
from mailer import send_digest, send_failure_alert

UK_TZ = ZoneInfo("Europe/London")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STATE_FILE = Path("state.json")
COMPANIES_FILE = Path("companies.csv")
DEFAULT_LOOKBACK_HOURS = 24  # used on very first run only

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
    return {"last_run": None, "seen_hashes": []}


def save_state(last_run: datetime, seen_hashes: set[str]) -> None:
    data = {
        "last_run": last_run.isoformat(),
        # Keep the last 10 000 hashes to cap file size
        "seen_hashes": list(seen_hashes)[-10_000:],
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
    logger.info("Monitoring %d companies", len(companies))

    # 3. Fetch RSS feeds
    logger.info("--- Fetching news feeds ---")
    raw_results: dict[str, list[NewsItem]] = fetch_all(companies, since, seen_hashes)
    total_fetched = sum(len(v) for v in raw_results.values())
    logger.info("Fetched %d new item(s) total", total_fetched)

    if total_fetched == 0:
        logger.info("No new items found — exiting cleanly (no email sent)")
        save_state(run_dt, seen_hashes)
        sys.exit(0)

    # 4. Summarise + filter with Anthropic
    logger.info("--- Classifying items with Anthropic ---")
    summariser = Summariser()

    # Flatten items for batch processing, carrying company name
    flat_items: list[NewsItem] = [
        item for items in raw_results.values() for item in items
    ]

    # Build dicts for summariser
    item_dicts = [
        {"company": item.company, "title": item.title, "source": item.source, "url": item.url}
        for item in flat_items
    ]
    classified = summariser.classify_batch(item_dicts)

    # Track all hashes as seen (relevant or not) to prevent re-processing
    new_hashes: set[str] = {item.item_hash for item in flat_items}

    # 5. Group relevant items by company
    digest: dict[str, list[dict]] = {}
    for news_item, (item_dict, result) in zip(flat_items, classified):
        if not result.relevant:
            continue
        company = news_item.company
        if company not in digest:
            digest[company] = []
        digest[company].append({
            "summary": result.summary,
            "url": news_item.url,
            "source": news_item.source,
            "published": news_item.published.strftime("%-d %b %Y"),
            "category": result.category,
        })

    total_relevant = sum(len(v) for v in digest.values())
    logger.info(
        "%d relevant item(s) across %d company/companies",
        total_relevant,
        len(digest),
    )

    # 6. Exit cleanly if nothing relevant
    if total_relevant == 0:
        logger.info("No relevant items after filtering — exiting cleanly (no email sent)")
        seen_hashes.update(new_hashes)
        save_state(run_dt, seen_hashes)
        sys.exit(0)

    # 7. Render and send digest
    logger.info("--- Sending digest ---")
    send_digest(digest, run_dt=run_dt, dry_run=dry_run)

    # 8. Persist state — update seen hashes AFTER successful send
    seen_hashes.update(new_hashes)
    save_state(run_dt, seen_hashes)

    logger.info("=== Done — %d relevant items sent ===", total_relevant)


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
