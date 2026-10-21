"""Amazon's public job search (amazon.jobs), the feed behind its careers site.

    GET https://www.amazon.jobs/en/search.json?category[]={board}
        &normalized_country_code[]=USA&sort=recent&offset=N&result_limit=100

Registry token: the job category slug, e.g. "software-development". The search
returns full descriptions, so no detail requests are needed. It refuses more
than 100 results per page and offsets beyond 10,000.
"""

import json
import re
from datetime import UTC, datetime
from typing import Any

from jobradar.collectors.base import CollectorError, JobCollector
from jobradar.collectors.public import overlapping_pages
from jobradar.models import Job
from jobradar.text import html_to_text

API_URL = "https://www.amazon.jobs/en/search.json"
SITE = "https://www.amazon.jobs"
# Kept from each search result; the rest is presentation (team images, the
# relative "updated N hours ago" label that changes on every poll, distance).
FIELDS = ("id", "id_icims", "title", "posted_date", "job_path", "location", "normalized_location",
          "locations", "country_code", "city", "state", "company_name", "business_category",
          "job_category", "job_family", "job_schedule_type", "is_intern", "university_job",
          "description", "basic_qualifications", "preferred_qualifications")


class AmazonCollector(JobCollector):
    source = "amazon"
    page_size = 100
    page_overlap = 10
    max_items = 10_000

    def collect(self) -> list[Job]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.board):
            raise CollectorError("expected an amazon.jobs category slug")
        self.complete_snapshot = True

        def fetch(offset: int) -> tuple[list, int]:
            payload = self.fetch_json(API_URL, {
                "category[]": self.board, "normalized_country_code[]": "USA", "sort": "recent",
                "offset": offset, "result_limit": self.page_size})
            if not isinstance(payload, dict) or payload.get("error"):
                raise CollectorError(f"unexpected amazon.jobs payload: {str(payload)[:80]}")
            return payload.get("jobs"), payload.get("hits")

        records = overlapping_pages(self, fetch, lambda r: r.get("id_icims") or None,
                                    page_size=self.page_size, overlap=self.page_overlap, max_items=self.max_items)
        return self.normalize_all([{k: r[k] for k in FIELDS if k in r} for r in records])

    def normalize(self, raw: dict[str, Any]) -> Job:
        sections = [("", raw.get("description")), ("Basic qualifications", raw.get("basic_qualifications")),
                    ("Preferred qualifications", raw.get("preferred_qualifications"))]
        description = "\n\n".join(f"{head}\n{html_to_text(body)}".strip()
                                  for head, body in sections if isinstance(body, str) and body.strip())
        path = raw["job_path"]
        if not isinstance(path, str) or not re.fullmatch(r"/[A-Za-z0-9/_.-]+", path):
            raise ValueError("unsafe job path")
        return Job(source=self.source, source_job_id=f"{self.board}:{raw['id_icims']}",
                   company=self.company or "Amazon", title=raw["title"],
                   location=raw.get("normalized_location") or raw.get("location"),
                   url=SITE + path, description=description,
                   posted_at=parse_posted_date(raw.get("posted_date")), raw=raw)

    @staticmethod
    def locations(raw: dict[str, Any]) -> list[str]:
        found = [raw.get("normalized_location"), raw.get("location"), raw.get("country_code")]
        for entry in raw.get("locations") or []:
            try:
                loc = json.loads(entry) if isinstance(entry, str) else entry
            except ValueError:
                continue
            if isinstance(loc, dict):
                found += [loc.get("normalizedLocation"), loc.get("city"), loc.get("normalizedStateName"),
                          loc.get("normalizedCountryCode"), loc.get("countryIso2a")]
        return [f for f in found if isinstance(f, str) and f]


def parse_posted_date(value: Any) -> datetime | None:
    """"October 21, 2026" -> midnight UTC (date-only; the age margin absorbs it)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%B %d, %Y").replace(tzinfo=UTC)
    except ValueError:
        return None
