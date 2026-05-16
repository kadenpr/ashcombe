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
target companies and decide whether they are genuinely significant.

RELEVANT categories (only these qualify):
  - senior_hire       : C-suite, director, VP appointments or departures
  - contract_win      : new contracts, framework awards, procurement wins
  - funding_ma        : fundraising rounds, M&A, acquisitions, mergers, IPO news
  - product_launch    : new products, services, platforms, or major product updates
  - partnership       : strategic alliances, joint ventures, teaming agreements
  - event             : keynote speeches, awards, major conference participation
  - financial_results : earnings releases, profit warnings, revenue announcements

NOT RELEVANT (suppress these):
  - Articles where the company is only briefly mentioned alongside many others
  - Generic industry commentary or opinion pieces that don't report a specific event
  - Sponsored content or press release syndication with no editorial value
  - Stock price moves without an underlying business event

DEDUPLICATION — if two or more items cover the same underlying story or event, \
mark only the single most informative one as relevant and mark the rest as not relevant.

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
Apply the criteria strictly.
"""

# Default model — swap to claude-opus-4-6 for higher accuracy if budget allows
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


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
