"""Eightfold AI career sites (Microsoft, Netflix and other large employers).

Two public listing APIs exist; a tenant may block one of them:
  pcsx: GET https://{host}/api/pcsx/search?domain=D&query=&start=N&sort_by=timestamp&<filters>
        -> data.positions / data.count (Microsoft)
  v2:   GET https://{host}/api/apply/v2/jobs?domain=D&start=N&num=10&sort_by=timestamp&<filters>
        -> positions / count (Netflix; pcsx search returns 403 there)
Both return 10 positions per page whatever is asked. Descriptions and the
publication time come from one detail request per posting:
        GET https://{host}/api/pcsx/position_details?position_id=ID&domain=D&hl=en -> data

Registry token: host:domain:mode:hex(extra query string), e.g. for Microsoft's
US software-engineering profession
  apply.careers.microsoft.com:microsoft.com:pcsx:<hex of
  "location=United States&filter_profession=software engineering">.
Filters keep the snapshot complete for that slice of the board (closures are
detected), unlike a keyword query.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode

from jobradar.collectors.base import CollectorError, JobCollector
from jobradar.collectors.public import hydrate, overlapping_pages
from jobradar.models import Job
from jobradar.text import html_to_text

log = logging.getLogger(__name__)

MODES = {"pcsx": ("/api/pcsx/search", "data"), "v2": ("/api/apply/v2/jobs", None)}
LISTING_FIELDS = ("id", "name", "posting_name", "locations", "standardizedLocations", "location", "postedTs",
                  "creationTs", "t_create", "department", "business_unit", "workLocationOption",
                  "work_location_option", "atsJobId", "ats_job_id", "displayJobId", "display_job_id")
DETAIL_FIELDS = ("name", "jobDescription", "postedTs", "creationTs", "locations", "standardizedLocations",
                 "location", "publicUrl", "workLocationOption", "efcustomTextEmploymentType",
                 "efcustomTextWorkSite")
# Eightfold's postedTs moves when a long-open requisition is re-posted (a
# Netflix role created in 2024 showed a postedTs from this week). A posting
# created long before its postedTs is dated by its creation time instead, so a
# re-post is never presented as a fresh job.
REPOST_GAP = timedelta(days=45)
DETAIL_MAX_AGE = timedelta(days=30)


def encode_board(host: str, domain: str, mode: str, query: dict[str, str] | None = None) -> str:
    board = ":".join([host.lower(), domain.lower(), mode, urlencode(query or {}).encode().hex()])
    decode_board(board)
    return board


def decode_board(board: str) -> tuple[str, str, str, list[tuple[str, str]]]:
    try:
        host, domain, mode, query_hex = board.split(":")
        query = parse_qsl(bytes.fromhex(query_hex).decode(), keep_blank_values=True, strict_parsing=bool(query_hex))
        name = r"[a-z0-9-]+(\.[a-z0-9-]+)+"
        if (not re.fullmatch(name, host) or not re.fullmatch(name, domain) or mode not in MODES
                or any(k in ("domain", "start", "num", "sort_by", "query") for k, _ in query)):
            raise ValueError("invalid part")
        return host, domain, mode, query
    except (ValueError, UnicodeError) as exc:
        raise CollectorError("expected Eightfold host:domain:mode:hex-query board token") from exc


def _ts(value: Any) -> datetime | None:
    if type(value) is not int or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class EightfoldCollector(JobCollector):
    source = "eightfold"
    page_size = 10       # fixed by the API
    page_overlap = 1
    max_items = 5_000
    reuses_known_details = "detail"
    title_first = True
    early_digest = True

    def collect(self) -> list[Job]:
        host, domain, mode, query = decode_board(self.board)
        path, envelope = MODES[mode]
        base = f"https://{host}"
        self.complete_snapshot = True

        def fetch(offset: int) -> tuple[list, int]:
            params = {"domain": domain, "start": offset, "sort_by": "timestamp", **dict(query)}
            if mode == "pcsx":
                params.setdefault("query", "")
            else:
                params["num"] = self.page_size
            payload = self.fetch_json(base + path, params)
            body = payload.get(envelope) if envelope and isinstance(payload, dict) else payload
            if not isinstance(body, dict):
                raise CollectorError("unexpected Eightfold search payload")
            return body.get("positions"), body.get("count")

        listed = overlapping_pages(self, fetch, lambda r: r.get("id") if type(r.get("id")) is int else None,
                                   page_size=self.page_size, overlap=self.page_overlap, max_items=self.max_items)
        if not listed and not self.complete_snapshot:
            raise CollectorError("no usable Eightfold postings in partial search results")
        records = [{k: r[k] for k in LISTING_FIELDS if k in r} for r in listed]
        self.check_listing_digest(records)
        now = datetime.now(UTC)

        def detail(item: dict) -> dict:
            stored = self.known.get(self._sid(item), {}).get("detail")
            listed_on = _ts(item.get("postedTs"))
            if stored or (listed_on and listed_on < now - DETAIL_MAX_AGE) or not self.needs_detail(_title(item)):
                return {**item, "detail": stored} if stored else item
            payload = self.fetch_json(base + "/api/pcsx/position_details",
                                      {"position_id": item["id"], "domain": domain, "hl": "en"})
            info = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(info, dict) or info.get("id") != item["id"]:
                raise CollectorError("invalid Eightfold position detail")
            return {**item, "detail": {k: info[k] for k in DETAIL_FIELDS if k in info}}

        return self.normalize_all(hydrate(self, records, detail))

    def _sid(self, raw: dict) -> str:
        # source_job_id is unique per source, so it is namespaced by board
        # (two filtered slices of one tenant must never claim the same row).
        return f"{self.board}:{int(raw['id'])}"

    def normalize(self, raw: dict[str, Any]) -> Job:
        host, domain, _, _ = decode_board(self.board)
        info = raw.get("detail") or {}
        url = info.get("publicUrl")
        if not (isinstance(url, str) and url.startswith(f"https://{host}/")):
            url = f"https://{host}/careers/job/{int(raw['id'])}"
        locations = info.get("locations") or raw.get("locations") or []
        return Job(source=self.source, source_job_id=self._sid(raw), company=self.company or domain.split(".")[0],
                   title=info.get("name") or _title(raw),
                   location=info.get("location") or (locations[0] if locations else None),
                   url=url, description=html_to_text(info.get("jobDescription")),
                   posted_at=publication_time(raw), raw=raw)

    @staticmethod
    def locations(raw: dict[str, Any]) -> list[str]:
        info = raw.get("detail") or {}
        found = []
        for source in (info, raw):
            for key in ("locations", "standardizedLocations"):
                found += [v for v in source.get(key) or [] if isinstance(v, str)]
            found.append(source.get("location"))
        return [f for f in found if isinstance(f, str) and f]


def _title(raw: dict) -> Any:
    return raw.get("name") or raw.get("posting_name")


def publication_time(raw: dict) -> datetime | None:
    """postedTs (detail, else listing); the creation time when postedTs is a re-post."""
    info = raw.get("detail") or {}
    posted = _ts(info.get("postedTs")) or _ts(raw.get("postedTs"))
    created = _ts(info.get("creationTs")) or _ts(raw.get("creationTs")) or _ts(raw.get("t_create"))
    if posted and created and posted - created > REPOST_GAP:
        return created
    return posted
