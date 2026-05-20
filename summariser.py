"""
summariser.py — Anthropic API wrapper for Ashcombe AI News Tracker.

For each news item, determines relevance and returns a structured summary.
Uses prompt caching on the system prompt to reduce token costs when
processing multiple items in a single run.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Relevance + summarisation prompt — tune this constant without touching logic
# ---------------------------------------------------------------------------
BATCH_SYSTEM_PROMPT = """\
You are a senior business intelligence analyst at Ashcombe Advisers, \
a specialist advisory firm. Your task is to assess news items about \
target companies and flag anything that could be of interest.

RELEVANT categories — include any item that fits one of these:
  - senior_hire       : C-suite, director, VP appointments or departures
  - contract_win      : new contracts, framework awards, procurement wins
  - funding_ma        : fundraising rounds, M&A, acquisitions, mergers, IPO news
  - product_launch    : new products, services, platforms, or product updates
  - partnership       : strategic alliances, joint ventures, teaming agreements
  - event             : awards, conference participation, speaking engagements
  - financial_results : earnings releases, profit warnings, revenue announcements
  - company_update    : any company news, announcements, strategic updates, \
operational milestones, customer wins, market commentary, or LinkedIn posts \
where the company discusses its business, products, customers, or team
  - media_coverage    : any press coverage, interviews, profiles, rankings, \
or features where the company is a primary subject

DEFAULT TO RELEVANT — if an item is about the company and contains any \
business substance, mark it relevant. Only suppress:
  - Items where the company is merely listed alongside 10+ other companies \
with no specific information about them
  - Pure stock price tickers with no news
  - Job adverts posted on LinkedIn (not news about hiring, just the advert itself)

DEDUPLICATION — if two or more items cover the same underlying story, \
mark only the most informative one as relevant.

You will receive multiple news items for the same company. \
Output ONLY a valid JSON array — no markdown, no prose — \
with one object per item in the same order:
[
  {
    "relevant": <true|false>,
    "summary": "<one sentence, max 20 words, active voice, present tense, or empty string if not relevant>",
    "category": "<one of the category keys above, or empty string if not relevant>"
  }
]
"""

BATCH_USER_TEMPLATE = """\
Company: {company}

{items}

Classify each item. Return a JSON array with {n} objects in the same order. \
When in doubt, mark as relevant.
"""

# Default model — swap to claude-opus-4-6 for higher accuracy if budget allows
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

DIGEST_SUMMARY_PROMPT = """\
You are a senior business intelligence analyst at Ashcombe Advisers. \
Write the opening summary for an internal intelligence digest. \
Be specific: name companies and describe what happened. Avoid vague language.

Output a single JSON object — no markdown, no extra prose:
{"summary": "<3-5 sentences, active voice, present tense>"}
"""

JOBS_EVAL_SYSTEM_PROMPT = """\
You are a senior business intelligence analyst at Ashcombe Advisers. \
A company's LinkedIn job posting data has changed. Evaluate what this signals \
about the company's strategic direction, growth trajectory, or operational priorities.

Output a single JSON object — no markdown, no extra prose:
{"evaluation": "<2-3 sentences, active voice, present tense, concrete and specific>"}
"""

PROFILE_EVAL_SYSTEM_PROMPT = """\
You are a senior business intelligence analyst at Ashcombe Advisers. \
A company has updated its LinkedIn profile. Evaluate what this change signals about \
the company's strategy, direction, or positioning.

Output a single JSON object — no markdown, no extra prose:
{"evaluation": "<2-3 sentences, active voice, present tense, concrete and specific>"}
"""


@dataclass
class SummaryResult:
    relevant: bool
    summary: str
    category: str


def _parse_response(text: str) -> SummaryResult:
    """Extract JSON from the model response, tolerating minor formatting noise."""
    # Strip any accidental markdown fences
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        data = json.loads(text)
        return SummaryResult(
            relevant=bool(data.get("relevant", False)),
            summary=str(data.get("summary", "")).strip(),
            category=str(data.get("category", "")).strip(),
        )
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse model response as JSON: %s\nRaw: %s", exc, text)
        return SummaryResult(relevant=False, summary="", category="")


class Summariser:
    """
    Wraps the Anthropic client to classify and summarise news items.

    Uses ephemeral prompt caching on the system prompt so that repeated
    calls within the same run share the cached prompt, cutting token costs.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"]
        )
        self._model = model

    def _classify_company_batch(
        self,
        company: str,
        items: list[dict],
    ) -> list[SummaryResult]:
        """
        Classify all items for a single company in one API call.
        Returns results in the same order as *items*.
        """
        lines = "\n".join(
            f"Item {i + 1} — Headline: {item['title']} | Source: {item['source']}"
            for i, item in enumerate(items)
        )
        user_content = BATCH_USER_TEMPLATE.format(
            company=company,
            items=lines,
            n=len(items),
        )

        fallback = [SummaryResult(relevant=False, summary="", category="") for _ in items]

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": BATCH_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text
            raw = re.sub(r"```(?:json)?", "", raw).strip()

            # Try direct parse first, then extract array-of-objects specifically.
            # The tighter pattern avoids capturing stray [Note: ...] text that
            # Claude occasionally adds before the actual JSON array.
            data = None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                match = re.search(r"\[\s*\{[\s\S]*\}\s*\]", raw)
                if match:
                    try:
                        data = json.loads(match.group())
                    except json.JSONDecodeError:
                        pass

            if data is None:
                logger.warning("No valid JSON array found in response for %s", company)
                return fallback
            if not isinstance(data, list):
                logger.warning("Batch response for %s was not a list", company)
                return fallback

            results = []
            for i, obj in enumerate(data[:len(items)]):
                results.append(SummaryResult(
                    relevant=bool(obj.get("relevant", False)),
                    summary=str(obj.get("summary", "")).strip(),
                    category=str(obj.get("category", "")).strip(),
                ))
                logger.debug(
                    "  [%s] item %d relevant=%s category=%s",
                    company, i + 1, results[-1].relevant, results[-1].category,
                )
            # Pad with fallback if model returned fewer objects than expected
            while len(results) < len(items):
                results.append(SummaryResult(relevant=False, summary="", category=""))
            return results

        except (anthropic.APIError, json.JSONDecodeError, Exception) as exc:
            logger.error("Batch classify error for %s: %s", company, exc)
            return fallback

    def classify_batch(
        self,
        items: list[dict],
    ) -> list[tuple[dict, SummaryResult]]:
        """
        Classify items grouped by company — one API call per company.
        Returns a list of (item, SummaryResult) tuples in the original order.
        """
        from collections import defaultdict

        # Group preserving original indices
        groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for i, item in enumerate(items):
            groups[item["company"]].append((i, item))

        results: list[tuple[dict, SummaryResult] | None] = [None] * len(items)

        for company, indexed_items in groups.items():
            indices = [i for i, _ in indexed_items]
            company_items = [item for _, item in indexed_items]
            company_results = self._classify_company_batch(company, company_items)
            for idx, result in zip(indices, company_results):
                results[idx] = (items[idx], result)

        return results  # type: ignore[return-value]

    def generate_digest_summary(
        self,
        digest: dict[str, list[dict]],
        profile_changes: list,
        jobs_changes: list,
        is_monday: bool,
    ) -> str:
        """
        Generate a 3-5 sentence executive summary of the full digest.
        Returns an empty string on failure (summary box is omitted from email).
        """
        lines: list[str] = []
        period = "this week" if is_monday else "today"

        n_news = sum(len(v) for v in digest.values())
        lines.append(
            f"Period: {period}. "
            f"{len(digest)} company/companies with relevant news ({n_news} item(s) total)."
        )

        if digest:
            lines.append("News highlights:")
            for company, items in digest.items():
                for item in items:
                    lines.append(
                        f"  - {company}: {item['summary']} [{item['category']}]"
                    )

        if profile_changes:
            lines.append("Company profile changes:")
            for c in profile_changes:
                lines.append(f"  - {c.company}: {c.field} updated")

        if jobs_changes:
            lines.append("Hiring activity changes:")
            for c in jobs_changes:
                delta = c.current.total - c.previous.total
                senior_note = (
                    f", new senior roles: {', '.join(c.new_senior_roles[:3])}"
                    if c.new_senior_roles
                    else ""
                )
                lines.append(
                    f"  - {c.company}: {c.current.total} open roles ({delta:+d}){senior_note}"
                )

        user_content = "\n".join(lines)

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=[
                    {
                        "type": "text",
                        "text": DIGEST_SUMMARY_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            raw = re.sub(r"```(?:json)?", "", response.content[0].text).strip()
            data = json.loads(raw)
            return str(data.get("summary", "")).strip()
        except Exception as exc:
            logger.error("Digest summary generation failed: %s", exc)
            return ""

    def evaluate_jobs_change(
        self,
        company: str,
        current_total: int,
        previous_total: int,
        new_senior_roles: list[str],
        top_functions: dict[str, int],
    ) -> str:
        """Return a 2-3 sentence strategic evaluation of a jobs snapshot change."""
        functions_text = ", ".join(
            f"{fn} ({n})" for fn, n in top_functions.items()
        ) or "unknown"
        senior_text = (
            "\n".join(f"  - {r}" for r in new_senior_roles)
            if new_senior_roles
            else "none"
        )
        user_content = (
            f"Company: {company}\n"
            f"Previous open positions: {previous_total}\n"
            f"Current open positions: {current_total} ({current_total - previous_total:+d})\n"
            f"New senior roles posted:\n{senior_text}\n"
            f"Current hiring by function: {functions_text}"
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=[
                    {
                        "type": "text",
                        "text": JOBS_EVAL_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            raw = re.sub(r"```(?:json)?", "", response.content[0].text).strip()
            data = json.loads(raw)
            return str(data.get("evaluation", "")).strip()
        except Exception as exc:
            logger.error("Jobs evaluation error for %s: %s", company, exc)
            return ""

    def evaluate_profile_change(
        self,
        company: str,
        field: str,
        old_value: str,
        new_value: str,
    ) -> str:
        """Return a 2-3 sentence strategic evaluation of a LinkedIn profile change."""
        user_content = (
            f"Company: {company}\n"
            f"Changed field: {field}\n\n"
            f"Previous text:\n{old_value}\n\n"
            f"New text:\n{new_value}"
        )
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=[
                    {
                        "type": "text",
                        "text": PROFILE_EVAL_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
            )
            raw = re.sub(r"```(?:json)?", "", response.content[0].text).strip()
            data = json.loads(raw)
            return str(data.get("evaluation", "")).strip()
        except Exception as exc:
            logger.error("Profile evaluation error for %s/%s: %s", company, field, exc)
            return ""


# ---------------------------------------------------------------------------
# Dry-run smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    summariser = Summariser()

    test_items = [
        {
            "company": "Balfour Beatty",
            "title": "Balfour Beatty wins £450m HS2 tunnelling contract",
            "source": "Construction News",
            "url": "https://example.com/balfour-hs2-contract",
        },
        {
            "company": "Serco Group",
            "title": "Ten UK companies report mixed results amid cost pressures",
            "source": "Financial Times",
            "url": "https://example.com/uk-companies-results",
        },
        {
            "company": "Atkins Global",
            "title": "Atkins Global appoints new Chief Digital Officer",
            "source": "Engineering Today",
            "url": "https://example.com/atkins-cdo",
        },
    ]

    print("=== Summariser dry-run ===\n")
    classified = summariser.classify_batch(test_items)
    for item, result in classified:
        print(f"Title   : {item['title']}")
        print(f"Company : {item['company']}")
        print(f"Relevant: {result.relevant}")
        if result.relevant:
            print(f"Summary : {result.summary}")
            print(f"Category: {result.category}")
        print()

    sys.exit(0)
