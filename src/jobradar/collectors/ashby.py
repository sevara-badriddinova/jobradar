"""Ashby public job posting API collector.

Public, unauthenticated endpoint
(https://developers.ashbyhq.com/docs/public-job-posting-api):
    GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true
Returns {"jobs": [...]} with every posting on the board in one response.
"""

import logging
from typing import Any

from jobradar.collectors.base import CollectorError, JobCollector, parse_iso_datetime
from jobradar.models import Job
from jobradar.text import html_to_text

log = logging.getLogger(__name__)

API_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"


class AshbyCollector(JobCollector):
    source = "ashby"

    def collect(self) -> list[Job]:
        # Compensation is free to request and useful later (filters/ranking);
        # it's kept in Job.raw.
        payload = self.fetch_json(API_URL.format(board=self.board), {"includeCompensation": "true"})
        records = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise CollectorError(f"unexpected Ashby payload for board {self.board!r}")

        # Unlisted postings aren't on the public board (reachable only by a
        # direct link, e.g. referral-only or not yet launched). Treat them as
        # not open. A job that becomes unlisted therefore gets closed.
        listed = [r for r in records if not (isinstance(r, dict) and r.get("isListed") is False)]
        if len(listed) != len(records):
            log.debug("ashby/%s: ignoring %d unlisted postings", self.board, len(records) - len(listed))
        return self.normalize_all(listed)

    def normalize(self, raw: dict[str, Any]) -> Job:
        return Job(
            source=self.source,
            source_job_id=str(raw["id"]),
            company=self.company or self.board,  # Ashby has no company-name field
            title=raw["title"],
            location=raw.get("location") if isinstance(raw.get("location"), str) else None,
            url=raw["jobUrl"],
            description=html_to_text(raw.get("descriptionHtml")) or (raw.get("descriptionPlain") or "").strip(),
            # Note: Ashby's publishedAt is the *latest* publication time, so it
            # can move forward when a posting is republished. first_seen_at (ours)
            # is the stable "when did we first see it".
            posted_at=parse_iso_datetime(raw.get("publishedAt")),
            raw=raw,
        )

    @staticmethod
    def locations(raw: dict[str, Any]) -> list[str]:
        secondary = [s for s in raw.get("secondaryLocations") or [] if isinstance(s, dict)]
        countries = [((place.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
                     for place in (raw, *secondary)]
        found = [s.get("location") for s in secondary] + countries
        return [f for f in found if isinstance(f, str) and f]
