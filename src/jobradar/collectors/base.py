import hashlib
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import httpx
from pydantic import ValidationError

from jobradar.http import FetchError, get_json, make_client, request_data
from jobradar.models import Job

log = logging.getLogger(__name__)


def parse_iso_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; None if missing or invalid.

    A timestamp without a timezone is taken as UTC rather than left naive:
    Postgres would otherwise interpret it in the *server's* timezone.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# Exact timestamps must be older than the window by this much more (time zones,
# date-only feeds that parse as midnight UTC) before a posting is skipped.
PUBLICATION_MARGIN = timedelta(days=1)


class CollectorError(Exception):
    """A collector could not produce a trustworthy snapshot."""


class UnchangedSnapshot(Exception):  # noqa: N818 - control flow, not an error
    """The source records equal the last ingested snapshot: nothing to normalize
    or ingest. Raised by normalize_all() when known_digest matches."""

    def __init__(self, digest: str, records: int) -> None:
        super().__init__(digest)
        self.digest, self.records = digest, records


def _normalizer_fingerprint() -> str:
    """Digest of the code that turns source records into stored rows, so a
    release that changes normalization never reuses an old snapshot digest."""
    root = Path(__file__).resolve().parent.parent
    files = sorted((root / "collectors").glob("*.py")) + [root / n for n in
                   ("models.py", "text.py", "job_age.py", "repository.py") if (root / n).exists()]
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


NORMALIZER_FINGERPRINT = _normalizer_fingerprint()


class JobCollector(ABC):
    """One source x one company board.

    The contract every source implements: fetch, normalize, return Jobs.
    Collectors never touch the database; persistence and dedup live elsewhere,
    so adding a source is purely "write collect() and normalize()".

    Behaviour that is the same for every source (HTTP client ownership, error
    translation, record-level failure isolation) lives here, once.
    """

    source: ClassVar[str]

    # True when collect() returns *every* open posting on the board. Only then
    # can "missing from this snapshot" be interpreted as "closed". A collector
    # that may miss postings (search-based, best-effort) must set this False.
    complete_snapshot: ClassVar[bool] = True

    # Whether Job.posted_at is a real publication time that may be used to skip
    # storing a never-seen posting as too old for the ingestion window.
    publication_date_trusted: ClassVar[bool] = True

    # Record keys that change on every poll without the posting changing
    # (excluded from the snapshot digest; the repository ignores them too).
    volatile_keys: ClassVar[tuple[str, ...]] = ()

    def known_older_than(self, job: Job, cutoff: datetime) -> bool:
        """True only when this posting is certainly published before `cutoff`.

        Missing, future, or untrusted dates are never "certainly old": such
        postings are always stored and left to the regular filters.
        """
        return (self.publication_date_trusted and job.posted_at is not None
                and job.posted_at < cutoff - PUBLICATION_MARGIN)

    def __init__(self, board: str, company: str | None = None,
                 client: httpx.Client | None = None) -> None:
        self.board = board.strip().lower()
        # The registry name wins over whatever the source calls the company,
        # so the same company gets the same name (and dedup key) on every source.
        self.company = company
        self._client = client  # shared, pooled client; otherwise one per request
        # Optional {source_job_id: stored raw subset} for postings already in the
        # database, so collectors can skip re-fetching details that cannot change.
        self.known: dict[str, dict] = {}
        # Snapshot digest support: set known_digest to the digest of the last
        # ingested snapshot to skip normalizing an identical one; digest_salt
        # carries ingestion parameters that change what would be stored.
        self.known_digest: str | None = None
        self.digest_salt: str = ""
        self.snapshot_digest: str | None = None

    @abstractmethod
    def collect(self) -> list[Job]:
        """Fetch and normalize. Raise CollectorError if the snapshot is unusable."""

    @abstractmethod
    def normalize(self, raw: dict[str, Any]) -> Job:
        """Convert one source record into a Job. May raise on malformed input."""

    @staticmethod
    def locations(raw: dict[str, Any]) -> list[str]:
        """Every location/country string in a stored raw payload besides Job.location.

        Static and based on `raw`, so filters can use it on jobs already in the DB.
        """
        return []

    def fetch_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        client = self._client or make_client()
        try:
            return get_json(client, url, params=params)
        except FetchError as exc:
            raise CollectorError(str(exc)) from exc
        finally:
            if client is not self._client:
                client.close()

    def records_digest(self, records: list[Any]) -> str | None:
        """Order-independent digest of the source records (volatile keys removed)."""
        if not records:
            return None
        volatile = set(self.volatile_keys)
        entries = sorted(json.dumps({k: v for k, v in r.items() if k not in volatile} if isinstance(r, dict) else r,
                                    sort_keys=True, ensure_ascii=True, default=str) for r in records)
        h = hashlib.sha256(json.dumps([NORMALIZER_FINGERPRINT, type(self).__name__, self.source, self.board,
                                       self.company, self.complete_snapshot, self.digest_salt]).encode())
        for entry in entries:
            h.update(b"\n")
            h.update(entry.encode())
        return h.hexdigest()

    def normalize_all(self, records: list[Any]) -> list[Job]:
        # Digest first: an identical listing needs no HTML-to-text parsing at all
        # (normalization was the largest CPU cost of unchanged polls).
        self.snapshot_digest = self.records_digest(records)
        if self.snapshot_digest is not None and self.snapshot_digest == self.known_digest:
            raise UnchangedSnapshot(self.snapshot_digest, len(records))
        jobs: list[Job] = []
        for raw in records:
            # Record-level failure isolation: one malformed posting is logged
            # and skipped rather than failing the whole board.
            if not isinstance(raw, dict):
                self.complete_snapshot = False
                log.warning("%s/%s: skipping non-object job entry %r", self.source, self.board, raw)
                continue
            try:
                jobs.append(self.normalize(raw))
            except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as exc:
                self.complete_snapshot = False
                log.warning("%s/%s: skipping malformed job %r: %s",
                            self.source, self.board, raw.get("id"), exc)
        if records and not jobs:
            # Everything failed to parse: almost certainly a format change on
            # their side. Treat as a failed fetch, NOT as "board is empty",
            # otherwise every job would be wrongly marked closed.
            raise CollectorError(f"could not normalize any of {len(records)} jobs")
        return jobs

    def fetch_data(self, url: str, **kwargs: Any) -> Any:
        """Retried read-only POST searches and text/XML feeds, with client reuse."""
        client = self._client or make_client()
        try:
            return request_data(client, url, **kwargs)
        except FetchError as exc:
            raise CollectorError(str(exc)) from exc
        finally:
            if client is not self._client:
                client.close()
