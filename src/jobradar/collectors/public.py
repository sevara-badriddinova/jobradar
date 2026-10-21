"""Small helpers for public ATS feeds; no authenticated recruiting APIs."""

import contextvars
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Callable

from jobradar.collectors.base import CollectorError, JobCollector

log = logging.getLogger(__name__)


def slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        raise CollectorError("invalid public board slug")
    return value


def require_records(payload: Any, key: str | None = None) -> list[dict]:
    records = payload.get(key) if key and isinstance(payload, dict) else payload if key is None else None
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise CollectorError("unexpected public board payload")
    return records


def hydrate(collector: JobCollector, records: list[dict], fetch: Callable[[dict], dict]) -> list[dict]:
    """Bound detail concurrency; a partial response must never close missing jobs."""
    def attempt(raw):
        try:
            return fetch(raw)
        except (CollectorError, KeyError, TypeError, ValueError) as exc:
            log.warning("%s/%s: detail unavailable: %s", collector.source, collector.board, exc)
            return None
    # Each detail request runs in a copy of the caller's context, so the
    # worker's byte budget (a ContextVar) also bounds these pool threads.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(contextvars.copy_context().run, attempt, raw) for raw in records]
        hydrated = [f.result() for f in futures]
    if any(r is None for r in hydrated):
        collector.complete_snapshot = False
    result = [r for r in hydrated if r is not None]
    if records and not result:
        raise CollectorError("no posting details could be fetched")
    return result


class _LinkedData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts: list[str] = []
        self.values: list[Any] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = dict(attrs).get("type", "").lower() == "application/ld+json"
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            try:
                self.values.append(json.loads("".join(self.parts)))
            except ValueError:
                pass
            self.active = False


def posting_metadata(value: Any) -> dict:
    """Only JobPosting schema supplies publication dates, never page update time."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            parser = _LinkedData()
            parser.feed(value)
            value = parser.values
    if isinstance(value, list):
        postings = [p for v in value if (p := posting_metadata(v))]
        # An ambiguous page with several different jobs cannot date this posting.
        return postings[0] if len(postings) == 1 else {}
    if isinstance(value, dict):
        types = value.get("@type", [])
        if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
            return value
        return posting_metadata(value.get("@graph", []))
    return {}


def location_strings(value: Any) -> list[str]:
    """Collect structured location labels without turning arbitrary fields into geography."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [s for v in value for s in location_strings(v)]
    if isinstance(value, dict):
        keys = ("city", "state", "region", "country", "countryCode", "country_code", "name",
                "label", "descriptor", "address", "addressCountry", "addressLocality", "addressRegion")
        return [s for k in keys for s in location_strings(value.get(k))]
    return []


def overlapping_pages(collector: JobCollector, fetch: Callable[[int], tuple[list, int]], key: Callable[[dict], Any],
                      *, page_size: int, overlap: int, max_items: int) -> list[dict]:
    """Offset pagination over a live, date-sorted search.

    Offsets are not a stable snapshot: a posting removed between two requests
    shifts everything after it left, so one posting would be skipped and then
    wrongly closed. Consecutive pages overlap by `overlap` entries and are
    de-duplicated by `key`, which absorbs up to that many removals per page.
    New postings at the top only shift entries right (seen twice, kept once).

    The snapshot stays complete only when every page was read and the unique
    count reached the total the source reported on its last page; otherwise
    `collector.complete_snapshot` is cleared so nothing is closed.
    """
    step = page_size - overlap
    if step <= 0:
        raise ValueError("overlap must be smaller than the page size")
    records: list[dict] = []
    seen: set = set()
    offset, total = 0, None
    while True:
        try:
            page, reported = fetch(offset)
            if not isinstance(page, list) or type(reported) is not int or reported < 0:
                raise CollectorError("invalid search page or total")
        except CollectorError:
            if not records:
                raise
            log.warning("%s/%s: page at offset %d unavailable; incomplete snapshot",
                        collector.source, collector.board, offset)
            collector.complete_snapshot = False
            break
        total = reported
        if total > max_items:
            # The source refuses deep offsets: a truncated listing would close
            # every posting beyond the cap.
            collector.complete_snapshot = False
            log.warning("%s/%s: %d postings exceed the %d the source can page through",
                        collector.source, collector.board, total, max_items)
        for item in page:
            k = key(item) if isinstance(item, dict) else None
            if k is None:
                collector.complete_snapshot = False
                log.warning("%s/%s: skipping entry without an id at offset %d", collector.source, collector.board, offset)
                continue
            if k not in seen:
                seen.add(k)
                records.append(item)
        if len(page) < page_size or offset + page_size >= min(total, max_items):
            break
        offset += step
    if total is not None and len(seen) < min(total, max_items):
        collector.complete_snapshot = False
        log.warning("%s/%s: %d unique postings of %d reported; incomplete snapshot",
                    collector.source, collector.board, len(seen), total)
    return records
