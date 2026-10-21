"""Oracle Recruiting Cloud (Oracle HCM Candidate Experience) public REST API.

Used by Oracle itself, JPMorgan Chase and many large employers. Listing:
    GET https://{host}.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber=S,locationId=L[,selectedCategoriesFacet=C],limit=200,offset=N,
                sortBy=POSTING_DATES_DESC
Details (description and exact publication time), one request per posting:
    GET .../recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="ID",siteNumber=S

Registry token: host:hex(site):locationId[:categoryId], e.g.
jpmc.fa:43585f31303031:300000000289738:300000086152753. The site number is
case-sensitive, so it is hex-encoded like Workday sites.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from jobradar.collectors.base import CollectorError, JobCollector, parse_iso_datetime
from jobradar.collectors.public import hydrate, location_strings, overlapping_pages
from jobradar.models import Job
from jobradar.text import html_to_text

log = logging.getLogger(__name__)

LIST_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
DETAIL_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
LISTING_FIELDS = ("Id", "Title", "PostedDate", "PostingEndDate", "PrimaryLocation", "PrimaryLocationCountry",
                  "WorkplaceType", "JobFamily", "secondaryLocations")
DETAIL_FIELDS = ("Title", "Category", "ExternalPostedStartDate", "ExternalPostedEndDate", "PrimaryLocation",
                 "PrimaryLocationCountry", "ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                 "ExternalQualificationsStr", "CorporateDescriptionStr", "workLocation", "otherWorkLocations",
                 "secondaryLocations")
# Listing dates are calendar days. Postings listed earlier than this are far
# outside any freshness window: their details are not fetched.
DETAIL_MAX_AGE = timedelta(days=30)


def encode_board(host: str, site: str, location_id: str, category_id: str | None = None) -> str:
    parts = [host.lower(), site.encode().hex(), location_id] + ([category_id] if category_id else [])
    board = ":".join(parts)
    decode_board(board)
    return board


def decode_board(board: str) -> tuple[str, str, str, str | None]:
    parts = board.split(":")
    try:
        if len(parts) not in (3, 4):
            raise ValueError("wrong number of parts")
        host, site_hex, location_id = parts[:3]
        category_id = parts[3] if len(parts) == 4 else None
        site = bytes.fromhex(site_hex).decode()
        if (not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.fa(\.[a-z0-9-]+)*", host)
                or not re.fullmatch(r"[A-Za-z0-9_]+", site) or site.encode().hex() != site_hex
                or not location_id.isdigit() or category_id is not None and not category_id.isdigit()):
            raise ValueError("invalid part")
        return host, site, location_id, category_id
    except (ValueError, UnicodeError) as exc:
        raise CollectorError("expected Oracle HCM host:hex-site:locationId[:categoryId] board token") from exc


class OracleHcmCollector(JobCollector):
    source = "oracle"
    page_size = 200
    page_overlap = 10
    max_items = 20_000
    reuses_known_details = "detail"
    title_first = True
    early_digest = True

    def collect(self) -> list[Job]:
        host, site, location_id, category_id = decode_board(self.board)
        base = f"https://{host}.oraclecloud.com"
        self.complete_snapshot = True

        def fetch(offset: int) -> tuple[list, int]:
            finder = f"findReqs;siteNumber={site},locationId={location_id}"
            if category_id:
                finder += f",selectedCategoriesFacet={category_id}"
            finder += f",limit={self.page_size},offset={offset},sortBy=POSTING_DATES_DESC"
            payload = self.fetch_json(base + LIST_PATH, {
                "onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder})
            items = payload.get("items") if isinstance(payload, dict) else None
            search = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else None
            if search is None:
                raise CollectorError("unexpected Oracle HCM search payload")
            return search.get("requisitionList"), search.get("TotalJobsCount")

        listed = overlapping_pages(self, fetch, lambda r: r.get("Id") or None,
                                   page_size=self.page_size, overlap=self.page_overlap, max_items=self.max_items)
        if not listed and not self.complete_snapshot:
            raise CollectorError("no usable Oracle HCM postings in partial search results")
        records = [{k: r[k] for k in LISTING_FIELDS if k in r} for r in listed]
        self.check_listing_digest(records)
        now = datetime.now(UTC)

        def detail(item: dict) -> dict:
            listed_on = _date(item.get("PostedDate"))
            stored = self.known.get(self._sid(item), {}).get("detail")
            if stored or (listed_on and listed_on < now - DETAIL_MAX_AGE) or not self.needs_detail(item.get("Title")):
                # Stored details cannot change without a new listing date; keep
                # them so an irrelevant or old posting is not seen as "changed".
                return {**item, "detail": stored} if stored else item
            payload = self.fetch_json(base + DETAIL_PATH, {
                "expand": "all", "onlyData": "true", "finder": f'ById;Id="{item["Id"]}",siteNumber={site}'})
            found = payload.get("items") if isinstance(payload, dict) else None
            info = found[0] if isinstance(found, list) and found and isinstance(found[0], dict) else None
            if info is None or not info.get("Title"):
                raise CollectorError("invalid Oracle HCM detail")
            return {**item, "detail": {k: info[k] for k in DETAIL_FIELDS if k in info}}

        hydrated = hydrate(self, records, detail)
        return self.normalize_all([r for r in hydrated if not _expired(r, now)])

    def _sid(self, raw: dict) -> str:
        return f"{self.board}:{raw['Id']}"

    def normalize(self, raw: dict[str, Any]) -> Job:
        host, site, _, _ = decode_board(self.board)
        info = raw.get("detail") or {}
        sections = [info.get("ExternalDescriptionStr"), info.get("ExternalResponsibilitiesStr"),
                    info.get("ExternalQualificationsStr"), info.get("CorporateDescriptionStr")]
        req_id = str(raw["Id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]+", req_id):
            raise ValueError("unsafe requisition id")
        return Job(source=self.source, source_job_id=self._sid(raw), company=self.company or host.split(".")[0],
                   title=info.get("Title") or raw["Title"],
                   location=info.get("PrimaryLocation") or raw.get("PrimaryLocation"),
                   url=f"https://{host}.oraclecloud.com/hcmUI/CandidateExperience/en/sites/{site}/job/{req_id}",
                   description="\n\n".join(html_to_text(s) for s in sections if isinstance(s, str) and s.strip()),
                   posted_at=parse_iso_datetime(info.get("ExternalPostedStartDate")) or _date(raw.get("PostedDate")),
                   raw=raw)

    @staticmethod
    def locations(raw: dict[str, Any]) -> list[str]:
        info = raw.get("detail") or {}
        found = [raw.get("PrimaryLocation"), raw.get("PrimaryLocationCountry"), info.get("PrimaryLocationCountry")]
        found += location_strings(raw.get("secondaryLocations")) + location_strings(info.get("secondaryLocations"))
        for loc in (info.get("workLocation") or []) + (info.get("otherWorkLocations") or []):
            if isinstance(loc, dict):
                found += [loc.get("TownOrCity"), loc.get("Region2"), loc.get("Country")]
        for loc in raw.get("secondaryLocations") or []:
            if isinstance(loc, dict):
                found.append(loc.get("CountryCode"))
        return [f for f in found if isinstance(f, str) and f]


def _date(value: Any) -> datetime | None:
    """Listing dates are "YYYY-MM-DD" calendar days."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _expired(raw: dict, now: datetime) -> bool:
    """A posting whose external end date has passed is closed even if still listed."""
    end = parse_iso_datetime((raw.get("detail") or {}).get("ExternalPostedEndDate"))
    if end is None and (day := _date(raw.get("PostingEndDate"))) is not None:
        end = day + timedelta(days=1)   # open through its last calendar day
    return end is not None and end < now
