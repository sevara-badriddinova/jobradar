"""HTTP with retries, exponential backoff and jitter."""

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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


class BackpressureError(FetchError):
    """The shared response byte budget stayed full; try the board again later."""


class ByteBudget:
    """Process-wide cap on response bytes held by in-flight collections.

    A request larger than the whole budget is clamped to it, i.e. it runs only
    when nothing else holds budget (one oversized board at a time).
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = int(capacity)
        self.used = 0
        self.peak = 0
        self._cond = threading.Condition()

    def acquire(self, n: int, timeout: float) -> bool:
        n = min(int(n), self.capacity)
        deadline = time.monotonic() + timeout
        with self._cond:
            while self.used + n > self.capacity:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self.used += n
            self.peak = max(self.peak, self.used)
            return True

    def release(self, n: int) -> None:
        with self._cond:
            self.used = max(0, self.used - int(n))
            self._cond.notify_all()


class ResponseBytes:
    """One collection's share of a ByteBudget, grown as its responses stream in.

    Parsed pages stay referenced until ingestion finishes, so the charge is the
    cumulative size of every response the collection read, held until close().
    """

    def __init__(self, budget: ByteBudget, max_response: int, wait: float = 60.0) -> None:
        self.budget, self.max_response, self.wait = budget, int(max_response), wait
        self.held = 0       # budget currently reserved
        self.total = 0      # decoded bytes read so far
        self.backpressure = False
        self._lock = threading.RLock()  # detail fetches charge from several threads

    def reserve(self, n: int) -> None:
        with self._lock:
            target = min(int(n), self.budget.capacity)
            if target <= self.held:
                return
            if not self.budget.acquire(target - self.held, self.wait):
                self.backpressure = True
                raise BackpressureError(f"byte budget exhausted ({self.budget.used}/{self.budget.capacity})")
            self.held = target

    def charge(self, n: int) -> None:
        with self._lock:
            self.total += n
            self.reserve(self.total)

    def close(self) -> None:
        with self._lock:
            if self.held:
                self.budget.release(self.held)
                self.held = 0


_response_bytes: ContextVar[ResponseBytes | None] = ContextVar("jobradar_response_bytes", default=None)


@contextmanager
def byte_bounded(budget: ByteBudget, *, max_response: int, reserve: int = 0,
                 wait: float = 60.0) -> Iterator[ResponseBytes]:
    """Within this block, every request in this thread streams under the budget."""
    task = ResponseBytes(budget, max_response, wait)
    token = _response_bytes.set(task)
    try:
        if reserve:
            task.reserve(reserve)
        yield task
    finally:
        _response_bytes.reset(token)
        task.close()


def _bounded_request(client: httpx.Client, task: ResponseBytes, method: str, url: str,
                     params: dict[str, Any] | None, json_body: dict[str, Any] | None) -> httpx.Response:
    request = client.build_request(method, url, params=params, json=json_body)
    resp = client.send(request, stream=True)
    try:
        if resp.status_code >= 400:
            return resp  # status and headers are all the caller reads
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > task.max_response:
            raise FetchError(f"response from {url} exceeds {task.max_response} bytes (not retryable)")
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > task.max_response:
                raise FetchError(f"response from {url} exceeds {task.max_response} bytes (not retryable)")
            task.charge(len(chunk))
            chunks.append(chunk)
    finally:
        resp.close()
    headers = [(k, v) for k, v in resp.headers.multi_items()
               if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
    return httpx.Response(resp.status_code, headers=headers, content=b"".join(chunks), request=request)


# Requests per second per provider family (shared by every board on that host).
PROVIDER_RATES = {
    "myworkdayjobs.com": 2.0, "greenhouse.io": 4.0, "ashbyhq.com": 2.0, "lever.co": 2.0,
    "rippling.com": 1.0, "personio.de": 1.0, "personio.com": 1.0, "recruitee.com": 2.0,
    "workable.com": 1.0,
}
DEFAULT_RATE = 1.0


def provider_key(host: str) -> str:
    host = (host or "").lower()
    for suffix in PROVIDER_RATES:
        if host == suffix or host.endswith("." + suffix):
            return suffix
    return host


class RateLimiter:
    """Thread-safe token buckets per provider (burst of 2, refill at its rate)."""

    def __init__(self, rates: dict[str, float] | None = None, default: float = DEFAULT_RATE,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.rates, self.default = dict(PROVIDER_RATES if rates is None else rates), default
        self._clock, self._sleep = clock, sleep
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()
        self.waited = 0.0

    def wait(self, key: str) -> float:
        rate = self.rates.get(key, self.default)
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (2.0, now))
            tokens = min(2.0, tokens + (now - last) * rate) - 1.0
            self._buckets[key] = (tokens, now)
            delay = 0.0 if tokens >= 0 else -tokens / rate
        if delay:
            self.waited += delay
            self._sleep(delay)
        return delay

    def hook(self, request: httpx.Request) -> None:
        self.wait(provider_key(request.url.host))


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


def request_data(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    as_text: bool = False,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    error = ""
    for attempt in range(max_attempts):
        retry_after = None
        try:
            task = _response_bytes.get()
            if task is None:
                resp = client.request(method, url, params=params, json=json_body)
            else:
                resp = _bounded_request(client, task, method, url, params, json_body)
        except httpx.TransportError as exc:  # DNS, connect, read timeout, reset...
            error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code < 400:
                if as_text:
                    return resp.text
                try:
                    return resp.json()
                except ValueError as exc:
                    # Public ATS endpoints sometimes return a temporary HTML
                    # maintenance page with HTTP 200. Retry once, not forever.
                    if attempt + 1 >= min(max_attempts, 2):
                        raise FetchError(f"invalid JSON from {url}") from exc
                    error = "invalid JSON"
            elif resp.status_code not in RETRYABLE_STATUS:
                raise FetchError(f"HTTP {resp.status_code} from {url} (not retryable)")
            else:
                error = f"HTTP {resp.status_code}"
                retry_after = _retry_after_seconds(resp)

        if attempt + 1 < max_attempts:
            delay = min(max_delay, backoff_delay(attempt, base_delay, max_delay)
                        if retry_after is None else retry_after)
            log.warning("%s %s failed (%s); retry %d/%d in %.1fs",
                        method, url, error, attempt + 1, max_attempts - 1, delay)
            (sleep or time.sleep)(delay)

    raise FetchError(f"{method} {url} failed after {max_attempts} attempts: {error}")


def get_json(client: httpx.Client, url: str, **kwargs: Any) -> Any:
    return request_data(client, url, **kwargs)
