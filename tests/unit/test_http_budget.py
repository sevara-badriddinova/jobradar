"""Byte-bounded ingestion and per-provider rate limits."""

import threading

import httpx
import pytest

from jobradar.http import (
    BackpressureError, ByteBudget, FetchError, RateLimiter, byte_bounded, get_json, provider_key,
)


def client_returning(body: bytes, headers=None):
    def handle(request):
        return httpx.Response(200, content=body, headers=headers or {"Content-Type": "application/json"})
    return httpx.Client(transport=httpx.MockTransport(handle))


def test_unbounded_path_is_unchanged():
    assert get_json(client_returning(b'{"a": 1}'), "https://x/y") == {"a": 1}


def test_bounded_request_parses_and_charges_the_budget():
    budget = ByteBudget(1000)
    with byte_bounded(budget, max_response=500) as meter:
        assert get_json(client_returning(b'{"jobs": [1, 2, 3]}'), "https://x/y") == {"jobs": [1, 2, 3]}
        assert meter.total == 19 and budget.used == 19
    assert budget.used == 0  # released when the collection ends


def test_oversized_streamed_response_fails_without_retry():
    calls = []

    def handle(request):
        calls.append(1)
        return httpx.Response(200, content=b"[" + b"1," * 400 + b"1]")
    client = httpx.Client(transport=httpx.MockTransport(handle))
    with byte_bounded(ByteBudget(10_000), max_response=100):
        with pytest.raises(FetchError, match="exceeds 100 bytes"):
            get_json(client, "https://x/big")
    assert len(calls) == 1


def test_declared_length_over_the_cap_is_rejected_before_reading():
    client = client_returning(b"{}", headers={"Content-Length": "999999999"})
    with byte_bounded(ByteBudget(10_000), max_response=1000):
        with pytest.raises(FetchError, match="exceeds"):
            get_json(client, "https://x/declared")


def test_full_budget_applies_backpressure_then_recovers():
    budget = ByteBudget(100)
    with byte_bounded(budget, max_response=10_000, reserve=100):   # an oversized board holds it all
        with pytest.raises(BackpressureError):
            with byte_bounded(budget, max_response=10_000, wait=0.05) as other:
                get_json(client_returning(b'{"a": 1}'), "https://x/y")
        assert other.backpressure and budget.used == 100
    with byte_bounded(budget, max_response=10_000, wait=0.05):
        assert get_json(client_returning(b'{"a": 1}'), "https://x/y") == {"a": 1}


def test_waiters_are_woken_when_budget_is_released():
    budget = ByteBudget(10)
    assert budget.acquire(10, 0)
    got = []
    t = threading.Thread(target=lambda: got.append(budget.acquire(5, 2)))
    t.start()
    budget.release(10)
    t.join()
    assert got == [True] and budget.peak == 10


def test_retryable_status_still_retries_under_the_budget():
    responses = iter([httpx.Response(503), httpx.Response(200, json={"ok": True})])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(responses)))
    with byte_bounded(ByteBudget(1000), max_response=1000):
        assert get_json(client, "https://x/y", sleep=lambda s: None) == {"ok": True}


def test_provider_keys_group_tenant_hosts():
    assert provider_key("nike.wd1.myworkdayjobs.com") == "myworkdayjobs.com"
    assert provider_key("boards-api.greenhouse.io") == "greenhouse.io"
    assert provider_key("example.com") == "example.com"


def test_token_bucket_spaces_requests_per_provider():
    clock, slept = [0.0], []

    def sleep(s):
        slept.append(s)
        clock[0] += s
    limiter = RateLimiter({"a": 2.0}, clock=lambda: clock[0], sleep=sleep)
    for _ in range(5):
        limiter.wait("a")
    assert slept == pytest.approx([0.5, 0.5, 0.5])     # burst of 2, then 2/s
    assert limiter.wait("b") == 0                        # other providers are independent
