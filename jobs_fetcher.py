"""
jobs_fetcher.py — Stub module for Ashcombe AI News Tracker.

LinkedIn job-posting scraping is not currently active.
Headcount changes are tracked via about_fetcher.py instead,
using the employee_count field from datadoping/linkedin-company-scraper.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class JobsSnapshot:
    total: int
    senior_roles: list[str]
    functions: dict[str, int]


@dataclass
class JobsChange:
    company: str
    current: JobsSnapshot
    previous: JobsSnapshot
    evaluation: str = field(default="")

    @property
    def count_delta(self) -> int:
        return self.current.total - self.previous.total

    @property
    def new_senior_roles(self) -> list[str]:
        prev_set = {r.lower() for r in self.previous.senior_roles}
        return [r for r in self.current.senior_roles if r.lower() not in prev_set]


def fetch_jobs_changes(
    companies: list[dict],  # noqa: ARG001
    stored_snapshots: dict,
) -> tuple[list[JobsChange], dict]:
    """No-op stub — returns no changes and passes snapshots through unchanged."""
    return [], stored_snapshots
