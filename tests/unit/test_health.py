from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobradar.health import CloudHealth, backup_diagnosis, check, diagnosis
from jobradar.telegram import TelegramError

NOW = datetime(2026, 10, 14, 14, tzinfo=UTC)


def test_backup_requires_recent_complete_verified_archive():
    good = dict(completed_at=NOW.isoformat(), archive_verified=True, bytes=1234)
    assert backup_diagnosis(good, NOW) is None
    assert backup_diagnosis(good, NOW+timedelta(hours=37))
    assert backup_diagnosis({}, NOW)
    assert backup_diagnosis({**good, "archive_verified": False}, NOW)
    assert backup_diagnosis({**good, "completed_at": "invalid"}, NOW)
    assert backup_diagnosis({**good, "bytes": 0}, NOW)


def execution(minutes=1, success=True, complete=True):
    return dict(createTime=(NOW-timedelta(minutes=minutes+1)).isoformat(), taskCount=1,
                succeededCount=int(success), **({"completionTime": (NOW-timedelta(minutes=minutes)).isoformat()} if complete else {}))


def test_detects_failures_missing_runs_and_overlaps_without_progress():
    assert diagnosis([execution(i, False) for i in (1, 6, 11)], NOW, last_progress=(NOW-timedelta(minutes=12)).isoformat())
    assert diagnosis([], NOW)
    assert diagnosis([execution()], NOW)  # skip exits cannot fake a heartbeat
    assert diagnosis([execution(1, complete=False)], NOW)
    assert diagnosis([execution()], NOW, last_progress=(NOW-timedelta(minutes=31)).isoformat())
    assert diagnosis([execution()], NOW, last_progress=(NOW-timedelta(minutes=1)).isoformat()) is None
    assert diagnosis([execution(i, False) for i in (1, 6, 11)], NOW,
                     last_progress=(NOW-timedelta(minutes=1)).isoformat()) is None  # isolated board errors


class FakeCloud:
    def __init__(self):
        self.state, self.generation, self.runs, self.heartbeat = {}, "0", [], None
        self.conflict = False

    def read(self):
        return dict(self.state), self.generation

    def write(self, state, generation):
        if self.conflict or generation != self.generation:
            return None
        self.state, self.generation = dict(state), str(int(generation)+1)
        return self.generation

    def executions(self):
        return self.runs

    def progress(self):
        return self.heartbeat


class FakeTelegram:
    def __init__(self, error=None):
        self.messages, self.error = [], error

    def send_message(self, chat, body, **kwargs):
        if self.error:
            raise self.error
        self.messages.append(body)


def test_one_outage_and_one_recovery_across_repeated_checks():
    cloud, tg = FakeCloud(), FakeTelegram()
    for _ in range(3):
        assert check(cloud, tg, "42", now=NOW) == 0
    assert len(tg.messages) == 1 and "needs attention" in tg.messages[0]
    cloud.runs, cloud.heartbeat = [execution()], NOW.isoformat()
    for _ in range(3):
        check(cloud, tg, "42", now=NOW)
    assert len(tg.messages) == 2 and "recovered" in tg.messages[1]


def test_healthy_baseline_and_concurrent_claim_are_quiet():
    cloud, tg = FakeCloud(), FakeTelegram()
    cloud.runs, cloud.heartbeat = [execution()], NOW.isoformat()
    check(cloud, tg, "42", now=NOW)
    assert not tg.messages
    cloud.heartbeat, cloud.conflict = None, True
    check(cloud, tg, "42", now=NOW)
    assert not tg.messages


@pytest.mark.parametrize("uncertain", [False, True])
def test_delivery_failure_retries_only_when_definitely_not_delivered(uncertain):
    cloud, tg = FakeCloud(), FakeTelegram(TelegramError("failure", maybe_delivered=uncertain))
    assert check(cloud, tg, "42", now=NOW) == 1
    tg.error = None
    check(cloud, tg, "42", now=NOW)
    assert len(tg.messages) == (0 if uncertain else 1)


def test_gcs_generation_preconditions_and_no_database_access():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "test-token"})
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.method == "POST":
            assert request.url.params["ifGenerationMatch"] == "0"
            return httpx.Response(412)
        return httpx.Response(404)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        cloud = CloudHealth(client, "p", "r", "j", "b")
        assert cloud.read() == ({}, "0")
        assert cloud.write({"status": "down"}, "0") is None
    assert len(requests) == 3
