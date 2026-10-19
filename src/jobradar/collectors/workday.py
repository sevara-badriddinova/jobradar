"""Workday's public CXS search and posting details.

Registry token: shard:tenant:hex-encoded-case-sensitive-site, e.g.
wd1:acme:45787465726e616c for site External. Never lowercase the site path.
"""

import json
import logging
import re
from urllib.parse import quote

from jobradar.collectors.base import CollectorError, JobCollector, parse_iso_datetime
from jobradar.collectors.public import hydrate, location_strings, slug
from jobradar.models import Job
from jobradar.text import html_to_text

log = logging.getLogger(__name__)


def encode_board(shard: str, tenant: str, site: str) -> str:
    if not re.fullmatch(r"wd\d+", shard) or not re.fullmatch(r"[A-Za-z0-9_-]+", site):
        raise CollectorError("invalid Workday board")
    return f"{shard}:{slug(tenant)}:{site.encode().hex()}"


def decode_board(board: str) -> tuple[str, str, str]:
    try:
        shard, tenant, encoded = board.split(":")
        site = bytes.fromhex(encoded).decode()
        if encode_board(shard, tenant, site) != board:
            raise ValueError("noncanonical board")
        return shard, tenant, site
    except (ValueError, UnicodeError) as exc:
        raise CollectorError("expected Workday shard:tenant:hex-site board token") from exc


class WorkdayCollector(JobCollector):
    source = "workday"
    page_size = 20  # CXS rejects larger limits on some tenants
    max_pages = 1000

    def collect(self) -> list[Job]:
        shard, tenant, site = decode_board(self.board)
        self.complete_snapshot = True
        base = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        records, seen, pages = [], set(), set()
        offset = 0
        expected_total = None
        for _ in range(self.max_pages):
            try:
                payload = self.fetch_data(base + "/jobs", method="POST", json_body={
                    "appliedFacets": {}, "limit": self.page_size, "offset": offset, "searchText": ""})
                page = payload.get("jobPostings") if isinstance(payload, dict) else None
                total = payload.get("total") if isinstance(payload, dict) else None
                if not isinstance(page, list) or type(total) is not int or total < 0:
                    raise CollectorError("invalid Workday search page or total")
            except CollectorError as exc:
                if not records:
                    raise
                self._partial(f"page at offset {offset} unavailable: {exc}")
                break
            # CXS reports total=0 on later pages even when postings remain.
            # Retain the first page's count so page two cannot truncate a board.
            if expected_total is None:
                expected_total = total
            signature = json.dumps(page, sort_keys=True)
            if signature in pages or not page and offset < expected_total:
                self._partial(f"pagination stopped at offset {offset} of {expected_total}")
                break
            pages.add(signature)
            for item in page:
                path = item.get("externalPath") if isinstance(item, dict) else None
                if (not isinstance(path, str) or not re.fullmatch(r"/job/[^?#]+", path)
                        or any(part in (".", "..") for part in path.split("/"))):
                    self._partial(f"skipping entry without a safe posting path at offset {offset}")
                    continue
                if path not in seen:
                    seen.add(path)
                    records.append(item)
                else:
                    self._partial(f"overlapping search results at offset {offset}")
            offset += len(page)
            # Offsets count *all* entries, including malformed rows. Waiting for
            # unique valid IDs to reach total would overrun a partial listing.
            if offset >= expected_total:
                break
        else:
            self._partial("pagination limit reached")
        if not records and not self.complete_snapshot:
            raise CollectorError("no usable Workday postings in partial search results")

        def detail(item):
            # This relative label can safely rule out an old job, but cannot
            # establish a fresh publication timestamp. Unknown/localized labels
            # always get the absolute startDate from the detail endpoint.
            old = re.fullmatch(r"Posted (\d+)\+? Days Ago", item.get("postedOn", ""))
            if old and int(old[1]) > 3:
                return item
            payload = self.fetch_json(base + quote(item["externalPath"], safe="/-_"))
            info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
            if not isinstance(info, dict) or not info.get("title"):
                raise CollectorError("invalid Workday detail")
            return {**item, "jobPostingInfo": info}
        hydrated = hydrate(self, records, detail)
        return self.normalize_all([r for r in hydrated
            if r.get("jobPostingInfo", {}).get("posted") is not False
            and r.get("jobPostingInfo", {}).get("canApply") is not False])

    def _partial(self, reason: str) -> None:
        self.complete_snapshot = False
        log.warning("workday/%s: incomplete snapshot; %s", self.board, reason)

    def normalize(self, raw):
        shard, tenant, site = decode_board(self.board)
        info = raw.get("jobPostingInfo", {})
        return Job(source=self.source, source_job_id=self.board + ":" + raw["externalPath"],
                   company=self.company or tenant, title=info.get("title") or raw["title"],
                   location=info.get("location") or raw.get("locationsText"),
                   url=f"https://{tenant}.{shard}.myworkdayjobs.com/{site}" + raw["externalPath"],
                   description=html_to_text(info.get("jobDescription")),
                   posted_at=parse_iso_datetime(info.get("startDate")), raw=raw)

    @staticmethod
    def locations(raw):
        info = raw.get("jobPostingInfo", {})
        return location_strings([info.get("country"), info.get("additionalLocations"),
                                 info.get("jobRequisitionLocation")])
