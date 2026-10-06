from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from jobradar.http import FetchError, backoff_delay, get_json
from tests.factories import RecordingTransport

URL = "https://x.test/jobs"


def call(transport, **kw):
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    result = get_json(transport.client(), URL, **kw)
    return result, sleeps


def test_success_returns_json_without_sleeping():
    t = RecordingTransport(httpx.Response(200, json={"jobs": []}))
    result, sleeps = call(t, params={"content": "true"})
    assert result == {"jobs": []} and sleeps == []
    assert str(t.requests[0].url) == URL + "?content=true"


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_retryable_statuses_are_retried(status):
    t = RecordingTransport(httpx.Response(status), httpx.Response(200, json={"ok": 1}))
    result, sleeps = call(t)
    assert result == {"ok": 1} and len(t.requests) == 2 and len(sleeps) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_non_retryable_statuses_fail_immediately(status):
    t = RecordingTransport(httpx.Response(status))
    with pytest.raises(FetchError, match=f"HTTP {status}.*not retryable"):
        call(t)
    assert len(t.requests) == 1


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("reset"),
])
def test_transport_errors_are_retried(exc):
    t = RecordingTransport(exc, httpx.Response(200, json=[1]))
    result, _ = call(t)
    assert result == [1]


def test_gives_up_after_max_attempts_with_last_error():
    t = RecordingTransport(httpx.Response(503))
    with pytest.raises(FetchError, match="after 3 attempts: HTTP 503"):
        call(t, max_attempts=3)
    assert len(t.requests) == 3


def test_sleeps_between_attempts_but_not_after_the_last():
    t = RecordingTransport(httpx.ConnectError("x"))
    sleeps: list[float] = []
    with pytest.raises(FetchError):
        get_json(t.client(), URL, max_attempts=4, sleep=sleeps.append)
    assert len(sleeps) == 3


def test_retry_after_seconds_is_honored():
    t = RecordingTransport(httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={}))
    _, sleeps = call(t)
    assert sleeps == [7.0]


def test_retry_after_http_date_is_honored():
    when = datetime.now(timezone.utc) + timedelta(seconds=20)
    t = RecordingTransport(httpx.Response(503, headers={"Retry-After": format_datetime(when, usegmt=True)}),
                           httpx.Response(200, json={}))
    _, sleeps = call(t)
    assert 15 <= sleeps[0] <= 20


def test_retry_after_is_capped_by_max_delay():
    t = RecordingTransport(httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200, json={}))
    _, sleeps = call(t, max_delay=30)
    assert sleeps == [30]


def test_garbage_retry_after_falls_back_to_backoff():
    t = RecordingTransport(httpx.Response(429, headers={"Retry-After": "soon-ish"}), httpx.Response(200, json={}))
    _, sleeps = call(t, base_delay=1.0)
    assert 0 <= sleeps[0] <= 1.0


def test_invalid_json_is_a_fetch_error_not_retried():
    t = RecordingTransport(httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(FetchError, match="invalid JSON"):
        call(t)
    assert len(t.requests) == 1


@pytest.mark.parametrize("attempt,upper", [(0, 1), (1, 2), (2, 4), (3, 8), (10, 30)])
def test_backoff_delay_is_jittered_exponential_and_capped(attempt, upper):
    samples = [backoff_delay(attempt, base=1.0, cap=30.0) for _ in range(200)]
    assert all(0 <= s <= upper for s in samples)
    assert len(set(samples)) > 1  # actually random, not a fixed value
