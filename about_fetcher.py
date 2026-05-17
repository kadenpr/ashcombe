"""
about_fetcher.py — LinkedIn company profile monitor for Ashcombe AI News Tracker.

Scrapes company about sections via datadoping/linkedin-company-scraper and
returns changes vs the previously stored profiles in state.json.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ACTOR_ID = "datadoping/linkedin-company-scraper"
ACTOR_TIMEOUT_SECS = 300

# Fields to watch for changes, mapped to human-readable labels
WATCHED_FIELDS = {
    "description": "About section",
    "tagline":     "Tagline",
    "specialties": "Specialties",
}

# Report a headcount change only when it moves by at least this many people
# or this percentage (whichever threshold is hit first)
HEADCOUNT_ABS_THRESHOLD = 10
HEADCOUNT_PCT_THRESHOLD = 0.05   # 5 %


@dataclass
class ProfileChange:
    company:    str
    field:      str   # human-readable label, e.g. "About section"
    old_value:  str
    new_value:  str
    evaluation: str = field(default="")   # filled in by Summariser after fetch


def _normalise_url(url: str) -> str:
    return url.rstrip("/").lower()


def fetch_profile_changes(
    companies: list[dict],
    stored_profiles: dict,
) -> tuple[list[ProfileChange], dict]:
    """
    Scrape LinkedIn profiles for all companies with a linkedin_url.
    Returns (changes_since_last_run, updated_profiles_dict).

    On the very first run stored_profiles is empty, so no changes are reported
    but the baseline is saved for future comparison.
    """
    try:
        from apify_client import ApifyClient
    except ImportError:
        logger.error("apify-client not installed — skipping profile monitoring")
        return [], stored_profiles

    api_token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not api_token:
        logger.warning("APIFY_API_TOKEN not set — skipping profile monitoring")
        return [], stored_profiles

    company_urls = [
        c["linkedin_url"].strip()
        for c in companies
        if c.get("linkedin_url", "").strip()
    ]
    if not company_urls:
        logger.info("No linkedin_url entries in companies.csv — skipping profile monitoring")
        return [], stored_profiles

    url_to_name: dict[str, str] = {
        _normalise_url(c["linkedin_url"]): c["name"]
        for c in companies
        if c.get("linkedin_url", "").strip()
    }

    logger.info(
        "Checking LinkedIn profiles for %d companies via Apify (%s)...",
        len(company_urls),
        ACTOR_ID,
    )

    client = ApifyClient(api_token)
    try:
        run = client.actor(ACTOR_ID).call(
            run_input={"companies": company_urls},
            timeout_secs=ACTOR_TIMEOUT_SECS,
        )
    except Exception as exc:
        logger.error("Apify actor run failed: %s", exc)
        return [], stored_profiles

    changes: list[ProfileChange] = []
    updated_profiles = dict(stored_profiles)

    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        input_url = _normalise_url(item.get("input", ""))
        company_name = url_to_name.get(input_url)
        if not company_name:
            continue

        # Build current snapshot
        current: dict[str, str] = {}
        for f in WATCHED_FIELDS:
            val = item.get(f)
            current[f] = str(val).strip() if val else ""

        # Also capture employee count
        emp = item.get("employee_count") or (item.get("stats") or {}).get("employee_count")
        current["employee_count"] = str(int(emp)) if emp else ""

        previous = stored_profiles.get(company_name, {})

        # Detect changes (skip if no previous baseline yet)
        if previous:
            for f, label in WATCHED_FIELDS.items():
                old = previous.get(f, "")
                new = current[f]
                if old and new and old != new:
                    changes.append(ProfileChange(
                        company=company_name,
                        field=label,
                        old_value=old,
                        new_value=new,
                    ))
                    logger.info("  Profile change detected: %s — %s", company_name, label)

            # Headcount change detection
            old_emp_str = previous.get("employee_count", "")
            new_emp_str = current["employee_count"]
            if old_emp_str and new_emp_str:
                try:
                    old_emp = int(old_emp_str)
                    new_emp = int(new_emp_str)
                    delta = new_emp - old_emp
                    pct = abs(delta) / old_emp if old_emp else 0
                    if abs(delta) >= HEADCOUNT_ABS_THRESHOLD or pct >= HEADCOUNT_PCT_THRESHOLD:
                        changes.append(ProfileChange(
                            company=company_name,
                            field="Headcount",
                            old_value=f"{old_emp:,} employees",
                            new_value=f"{new_emp:,} employees ({delta:+,})",
                        ))
                        logger.info(
                            "  Headcount change: %s — %d → %d (%+d)",
                            company_name, old_emp, new_emp, delta,
                        )
                except (ValueError, TypeError):
                    pass

        updated_profiles[company_name] = current

    logger.info(
        "Profile check complete: %d change(s) across %d companies",
        len(changes),
        len(updated_profiles),
    )
    return changes, updated_profiles
