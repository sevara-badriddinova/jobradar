from types import SimpleNamespace

import pytest

from jobradar import cli
from jobradar.cycle import cycle_lock, run_cycle
from jobradar.db import make_engine


def options(**overrides):
    return SimpleNamespace(**(dict(profile="profile.toml", applications="none",
                                  inbox_limit=200, match=False, match_limit=25,
                                  notify=False, digest=False) | overrides))


@pytest.mark.parametrize("failure,expected", [
    (None, ["collect", "applications", "filter", "match", "notify"]),
    ("collect", ["collect", "applications", "filter", "match", "notify"]),
    ("applications", ["collect", "applications"]),
    ("filter", ["collect", "applications", "filter"]),
    ("match", ["collect", "applications", "filter", "match", "notify"]),
])
def test_stage_failures_and_dependency_order(failure, expected):
    calls = []

    def run(argv):
        calls.append(argv[2])
        return int(argv[2] == failure)

    assert run_cycle(options(applications="notion", match=True, notify=True), run) == bool(failure)
    assert calls == expected


def test_default_cycle_has_no_paid_calls_or_messages():
    calls = []
    assert run_cycle(options(), lambda argv: calls.append(argv) or 0) == 0
    assert [c[2] for c in calls] == ["collect", "filter"]


def test_empty_registry_stops_cycle():
    calls = []
    assert run_cycle(options(), lambda argv: calls.append(argv) or 2) == 2
    assert len(calls) == 1


def test_match_configuration_failure_holds_notifications():
    calls = []

    def run(argv):
        calls.append(argv[2])
        return 2 if argv[2] == "match" else 0

    assert run_cycle(options(match=True, notify=True), run) == 2
    assert calls == ["collect", "filter", "match"]


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg"])
def test_neon_url_uses_installed_driver_and_keeps_tls(scheme):
    engine = make_engine(f"{scheme}://user:secret@ep-example.neon.tech/jobradar?sslmode=require")
    assert engine.url.drivername == "postgresql+psycopg"
    assert engine.url.query["sslmode"] == "require"
    engine.dispose()


def test_pooled_neon_connection_rejected_before_connecting():
    engine = make_engine("postgresql://user:secret@ep-example-pooler.neon.tech/jobradar")
    with pytest.raises(ValueError, match="direct Neon"):
        with cycle_lock(engine):
            pytest.fail("must reject pooled endpoint")


@pytest.mark.parametrize("argv", [["--match-limit", "0"], ["--inbox-limit", "-1"]])
def test_cycle_budgets_must_be_positive(argv):
    with pytest.raises(SystemExit) as error:
        cli.main(["cycle", *argv])
    assert error.value.code == 2


def test_digest_requires_notify():
    assert cli.main(["cycle", "--digest"]) == 2


@pytest.mark.parametrize("failure,progress", [(None, True), ("collect", True),
    ("applications", False), ("filter", False), ("match", False), ("notify", False)])
def test_heartbeat_only_after_pipeline_reaches_final_stage(monkeypatch, failure, progress):
    from jobradar import health
    heartbeats = []
    monkeypatch.setattr(health, "record_progress", lambda: heartbeats.append(True))
    run_cycle(options(applications="notion", match=True, notify=True),
              lambda argv: int(argv[2] == failure))
    assert bool(heartbeats) == progress
