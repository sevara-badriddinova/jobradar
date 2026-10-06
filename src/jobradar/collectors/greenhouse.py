"""Greenhouse Job Board API collector.

Public, unauthenticated endpoint:
    GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true
It returns every published job on the board in one response (no pagination),
so a successful call is a complete snapshot of what's open right now.
"""

from typing import Any

from jobradar.collectors.base import CollectorError, JobCollector, parse_iso_datetime
from jobradar.models import Job
from jobradar.text import html_to_text

API_URL = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"


class GreenhouseCollector(JobCollector):
    source = "greenhouse"

    def collect(self) -> list[Job]:
        payload = self.fetch_json(API_URL.format(board=self.board), {"content": "true"})
        records = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise CollectorError(f"unexpected Greenhouse payload for board {self.board!r}")
        return self.normalize_all(records)

    def normalize(self, raw: dict[str, Any]) -> Job:
        location = raw.get("location") or {}
        return Job(
            source=self.source,
            source_job_id=str(raw["id"]),
            company=self.company or raw.get("company_name") or self.board,
            title=raw["title"],
            location=location.get("name") if isinstance(location, dict) else None,
            url=raw["absolute_url"],
            description=html_to_text(raw.get("content")),
            posted_at=(parse_iso_datetime(raw.get("first_published"))
                       or parse_iso_datetime(raw.get("updated_at"))),
            raw=raw,
        )

    @staticmethod
    def locations(raw: dict[str, Any]) -> list[str]:
        offices = raw.get("offices") or []
        return [o.get("location") or o.get("name") for o in offices
                if isinstance(o, dict) and (o.get("location") or o.get("name"))]
