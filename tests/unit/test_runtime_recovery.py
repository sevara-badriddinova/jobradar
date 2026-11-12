import importlib.util
import time
from pathlib import Path

import pytest

from jobradar.runtime import atomic_json, maintenance, read_json, status_ok, cleanup_permitted

spec = importlib.util.spec_from_file_location("recovery", Path(__file__).parents[2] / "deploy/vm/recovery.py")
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def healthy(**extra):
    return dict(checked_at=time.time(), state="running",
                stages={"filter": dict(alive=True, stalled=False, state="idle")}, **extra)


def test_maintenance_expires_and_rejects_invalid_deadlines(tmp_path):
    request = dict(kind="backup", token="test", started_at=100, expires_at=200)
    atomic_json(tmp_path / "maintenance.json", request)
    assert maintenance(tmp_path, now=150) == request
    assert not maintenance(tmp_path, now=200)
    assert not maintenance(tmp_path, now=99)
    for change in ({"expires_at": 99999}, {"kind": "unknown"}, {"token": ""}):
        atomic_json(tmp_path / "maintenance.json", {**request, **change})
        assert not maintenance(tmp_path, now=150)
    (tmp_path / "maintenance.json").write_text("truncated {")
    assert not maintenance(tmp_path, now=150)


def test_status_requires_fresh_live_stages():
    state = healthy()
    assert status_ok(state)
    assert not status_ok({**state, "checked_at": time.time() - 100})
    assert not status_ok({**state, "state": "stopping"})
    assert not status_ok({**state, "stages": {"filter": {"alive": False, "stalled": False}}})
    assert not status_ok({**state, "stages": {}})


def test_backup_waits_for_matching_ack_and_clears_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "CONTROL", tmp_path)
    seen = []
    def status():
        request = maintenance(tmp_path)
        seen.append(request)
        return healthy(quiesced=True, maintenance=request)
    monkeypatch.setattr(recovery, "worker_status", status)
    with pytest.raises(RuntimeError, match="dump failed"):
        with recovery.low_load_backup():
            assert maintenance(tmp_path)["kind"] == "backup"
            raise RuntimeError("dump failed")
    assert seen and not maintenance(tmp_path)


def test_backup_does_not_run_without_drain_ack(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "CONTROL", tmp_path)
    monkeypatch.setattr(recovery, "worker_status", lambda: healthy(quiesced=True, maintenance={"token": "old"}))
    with pytest.raises(RuntimeError, match="drain timed out"):
        with recovery.low_load_backup(timeout=0):
            pytest.fail("must not start backup")
    assert not maintenance(tmp_path)


def test_backup_refuses_existing_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "CONTROL", tmp_path)
    now = time.time()
    atomic_json(tmp_path / "maintenance.json", dict(kind="deploy", token="other", started_at=now, expires_at=now+100))
    with pytest.raises(RuntimeError, match="another maintenance"):
        with recovery.low_load_backup(timeout=0):
            pass
    assert maintenance(tmp_path)["token"] == "other"


def test_verification_rejects_stale_success_and_listener_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "CONTROL", tmp_path)
    (tmp_path / "status").mkdir()
    atomic_json(tmp_path / "status/worker.json", healthy())
    atomic_json(tmp_path / "status/listener.json", dict(checked_at=time.time(), state="running", error=None))
    recovery.verify(timeout=0)
    with pytest.raises(RuntimeError, match="verification failed"):
        recovery.verify(timeout=0, after=time.time()+1)
    atomic_json(tmp_path / "status/listener.json", dict(checked_at=time.time(), state="running", error="network"))
    with pytest.raises(RuntimeError):
        recovery.verify(timeout=0)


def test_repair_attempts_are_bounded_and_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "DATA", tmp_path)
    for now in (100, 101, 102):
        recovery.allow_repair(now)
    with pytest.raises(RuntimeError, match="three attempts"):
        recovery.allow_repair(103)
    recovery.allow_repair(2000)
    assert read_json(tmp_path / "recovery-attempts.json")["attempts"] == [2000]


def test_cleanup_requires_a_recent_verified_backup(tmp_path):
    from datetime import UTC, datetime
    # Measure the boundary from the timestamp actually stored in the marker.
    # Serializing time.time() to ISO rounds to microseconds on Linux and macOS.
    completed = datetime(2026, 11, 9, 8, 3, 42, 125000, tzinfo=UTC)
    now = completed.timestamp()
    assert not cleanup_permitted(tmp_path)
    value = dict(completed_at=completed.isoformat(), archive_verified=True, bytes=100)
    atomic_json(tmp_path / "backup.json", value)
    assert cleanup_permitted(tmp_path, now=now+1)
    assert cleanup_permitted(tmp_path, now=now+36*3600-0.000001)
    assert not cleanup_permitted(tmp_path, now=now+36*3600)
    atomic_json(tmp_path / "backup.json", {**value, "archive_verified": False})
    assert not cleanup_permitted(tmp_path, now=now+1)
