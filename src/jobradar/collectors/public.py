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
