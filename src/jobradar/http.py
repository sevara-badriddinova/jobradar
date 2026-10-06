"""HTTP with retries, exponential backoff and jitter."""

import logging
import random
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "jobradar/0.1 (personal job search; low-rate polling)"

# Worth retrying: rate limited, or the server/network had a transient problem.
# Not worth retrying: other 4xx (bad board token, auth) - retrying won't help.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchError(Exception):
    """Raised when a request ultimately fails (non-retryable, or retries exhausted)."""


def make_client() -> httpx.Client:
    return httpx.Client(
        timeout=20.0,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            return max(0.0, (when - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError):
            return None


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """'Full jitter' backoff: random in [0, min(cap, base * 2^attempt)].

    Jitter spreads retries out so many collectors that failed together don't
    all hammer the source again at the same instant (thundering herd).
    """
    return random.uniform(0, min(cap, base * (2 ** attempt)))


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    error = ""
    for attempt in range(max_attempts):
        retry_after = None
        try:
            resp = client.get(url, params=params)
        except httpx.TransportError as exc:  # DNS, connect, read timeout, reset...
            error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise FetchError(f"invalid JSON from {url}") from exc
            if resp.status_code not in RETRYABLE_STATUS:
                raise FetchError(f"HTTP {resp.status_code} from {url} (not retryable)")
            error = f"HTTP {resp.status_code}"
            retry_after = _retry_after_seconds(resp)

        if attempt + 1 < max_attempts:
            delay = min(max_delay, backoff_delay(attempt, base_delay, max_delay)
                        if retry_after is None else retry_after)
            log.warning("GET %s failed (%s); retry %d/%d in %.1fs",
                        url, error, attempt + 1, max_attempts - 1, delay)
            (sleep or time.sleep)(delay)

    raise FetchError(f"GET {url} failed after {max_attempts} attempts: {error}")
