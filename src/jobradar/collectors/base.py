import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from pydantic import ValidationError

from jobradar.http import FetchError, get_json, make_client
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


class CollectorError(Exception):
    """A collector could not produce a trustworthy snapshot."""


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

    def __init__(self, board: str, company: str | None = None,
                 client: httpx.Client | None = None) -> None:
        self.board = board.strip().lower()
        # The registry name wins over whatever the source calls the company,
        # so the same company gets the same name (and dedup key) on every source.
        self.company = company
        self._client = client  # shared, pooled client; otherwise one per request

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

    def normalize_all(self, records: list[Any]) -> list[Job]:
        jobs: list[Job] = []
        for raw in records:
            # Record-level failure isolation: one malformed posting is logged
            # and skipped rather than failing the whole board.
            if not isinstance(raw, dict):
                log.warning("%s/%s: skipping non-object job entry %r", self.source, self.board, raw)
                continue
            try:
                jobs.append(self.normalize(raw))
            except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as exc:
                log.warning("%s/%s: skipping malformed job %r: %s",
                            self.source, self.board, raw.get("id"), exc)
        if records and not jobs:
            # Everything failed to parse: almost certainly a format change on
            # their side. Treat as a failed fetch, NOT as "board is empty",
            # otherwise every job would be wrongly marked closed.
            raise CollectorError(f"could not normalize any of {len(records)} jobs")
        return jobs
