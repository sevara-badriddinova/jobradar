"""Lever Postings API collector.

Public, unauthenticated endpoint (https://github.com/lever/postings-api):
    GET https://api.lever.co/v0/postings/{site}?mode=json&skip=N&limit=M
Returns a bare JSON array. The docs don't define the default page size, so we
always paginate explicitly with skip/limit until a short page comes back.
"""

from datetime import UTC, datetime
from typing import Any

from jobradar.collectors.base import CollectorError, JobCollector
from jobradar.models import Job
from jobradar.text import html_to_text

API_URL = "https://api.lever.co/v0/postings/{board}"


class LeverCollector(JobCollector):
    source = "lever"

    page_size = 100
    # Offset pagination isn't a stable snapshot: if a posting is removed
    # between two page requests, everything after it shifts left and one
    # posting would be skipped (and then wrongly marked closed). Overlapping
    # pages, then de-duplicating by ID, absorbs up to `page_overlap` removals.
    page_overlap = 10
    max_pages = 50  # 5,000 postings: far beyond any real company; stops runaway loops

    def collect(self) -> list[Job]:
        board = self.board
        api = API_URL
        if board.startswith("eu:"):
            board = board[3:]
            api = api.replace("api.lever.co", "api.eu.lever.co")
        url = api.format(board=board)
        records: list[Any] = []
        seen: set[str | None] = set()
        for page_number in range(self.max_pages):
            skip = page_number * (self.page_size - self.page_overlap)
            page = self.fetch_json(url, {"mode": "json", "skip": skip, "limit": self.page_size})
            if not isinstance(page, list):
                raise CollectorError(f"unexpected Lever payload for site {self.board!r}")
            for raw in page:
                posting_id = raw.get("id") if isinstance(raw, dict) else None
                if posting_id is None or posting_id not in seen:  # id-less records go on to be logged
                    seen.add(posting_id)
                    records.append(raw)
            if len(page) < self.page_size:
                return self.normalize_all(records)
        # Never return a partial list as if it were complete: that would close
        # every posting we didn't reach.
        raise CollectorError(f"Lever site {self.board!r} exceeded {self.max_pages} pages")

    def normalize(self, raw: dict[str, Any]) -> Job:
        categories = raw.get("categories") or {}
        return Job(
            source=self.source,
            source_job_id=str(raw["id"]),
            company=self.company or self.board,  # Lever has no company-name field
            title=raw["text"],
            location=categories.get("location") if isinstance(categories, dict) else None,
            url=raw["hostedUrl"],
            description=_description(raw),
            posted_at=_from_millis(raw.get("createdAt")),
            raw=raw,
        )

    @staticmethod
    def locations(raw: dict[str, Any]) -> list[str]:
        categories = raw.get("categories")
        found = list(categories.get("allLocations") or []) if isinstance(categories, dict) else []
        found.append(raw.get("country"))  # ISO-2 code, e.g. "US"
        return [f for f in found if isinstance(f, str) and f]


def _from_millis(value: Any) -> datetime | None:
    # Lever timestamps are milliseconds since the Unix epoch.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _description(raw: dict[str, Any]) -> str:
    """Lever splits a posting into an intro, titled lists and a closing section.

    Rebuild one document from them so the downstream matcher sees everything,
    including the "Requirements" list, which usually holds years of experience.
    """
    parts = [html_to_text(raw.get("description")) or (raw.get("descriptionPlain") or "").strip()]
    for item in raw.get("lists") or []:
        if not isinstance(item, dict):
            continue
        heading = (item.get("text") or "").strip()
        body = html_to_text(item.get("content"))
        if heading or body:
            parts.append(f"{heading}\n{body}".strip())
    parts.append(html_to_text(raw.get("additional")) or (raw.get("additionalPlain") or "").strip())
    return "\n\n".join(p for p in parts if p)
