import base64
import hashlib
import importlib.util
import io
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from urllib.error import URLError

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "deploy/vm"))
spec = importlib.util.spec_from_file_location("vm_cloud_io", Path(__file__).parents[2] / "deploy/vm/cloud_io.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_rotation_stages_only_one_secret_and_requires_valid_provider_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "ROOT", tmp_path)
    original = "AI_BUDGET='1'\nANTHROPIC_API_KEY='old'\nTELEGRAM_BOT_TOKEN='unchanged'\n"
    runtime = tmp_path / "runtime.env"
    runtime.write_text(original)
    staged = tmp_path / "prepared.env"
    monkeypatch.setattr(backup, "token", lambda: "test-access")
    def fetch(name, access, version):
        assert (name, access, version) == ("jobradar-anthropic-key", "test-access", "7")
        return "new-test-key"
    monkeypatch.setattr(backup, "secret", fetch)
    def rejected(req, timeout):
        assert req.get_method() == "GET" and req.full_url == "https://api.anthropic.com/v1/models?limit=1"
        assert req.get_header("X-api-key") == "new-test-key"
        raise URLError("rejected")
    monkeypatch.setattr(backup, "urlopen", rejected)
    with pytest.raises(URLError):
        backup.prepare_rotated_env("7", staged)
    assert not staged.exists()
    assert runtime.read_text() == original
    monkeypatch.setattr(backup, "urlopen", lambda *a, **k: io.BytesIO(b'{"data":[{"id":"test"}]}'))
    backup.prepare_rotated_env("7", staged)
    assert staged.read_text() == original.replace("'old'", "'new-test-key'")
    assert staged.stat().st_mode & 0o777 == 0o600
    assert runtime.read_text() == original


def test_rotation_pins_a_number_and_rejects_bad_pins_before_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "ROOT", tmp_path)
    assert backup.anthropic_version() == "1"
    pin = tmp_path / "anthropic-secret-version"
    pin.write_text("7\n")
    assert backup.anthropic_version() == "7"
    for invalid in ["latest", "0", "01", "../2", ""]:
        pin.write_text(invalid)
        with pytest.raises(ValueError):
            backup.anthropic_version()
    seen = []
    def fetched(req, timeout):
        seen.append(req.full_url)
        return io.BytesIO(b'{"payload":{"data":"dGVzdA=="}}')
    monkeypatch.setattr(backup, "urlopen", fetched)
    backup.secret("jobradar-anthropic-key", "placeholder", "7")
    backup.secret("jobradar-telegram-token", "placeholder")
    assert seen[0].endswith("/jobradar-anthropic-key/versions/7:access")
    assert seen[1].endswith("/jobradar-telegram-token/versions/1:access")


def test_upload_streams_reopens_after_failure_and_checks_remote_digest(tmp_path, monkeypatch):
    path = tmp_path / "archive"
    payload = b"test archive" * 100_000
    path.write_bytes(payload)
    monkeypatch.setattr(backup, "token", lambda: "test")
    monkeypatch.setattr(backup.time, "sleep", lambda _: None)
    attempts = []
    def upload(request, timeout):
        assert not isinstance(request.data, bytes)
        assert int(request.headers["Content-length"]) == len(payload)
        assert request.data.read(20) == payload[:20]
        attempts.append(request)
        if len(attempts) == 1:
            raise URLError("interrupted after partial upload")
        assert request.data.read() == payload[20:]
        return io.BytesIO(json.dumps(dict(size=str(len(payload)), generation="123",
            md5Hash=base64.b64encode(hashlib.md5(payload).digest()).decode())).encode())
    monkeypatch.setattr(backup, "urlopen", upload)
    result = backup.upload_file(path, "vm-backups/day-0.dump")
    assert len(attempts) == 2
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["generation"] == "123"


def test_upload_does_not_accept_corrupt_object(tmp_path, monkeypatch):
    path = tmp_path / "archive"
    path.write_bytes(b"archive")
    monkeypatch.setattr(backup, "token", lambda: "test")
    monkeypatch.setattr(backup, "urlopen", lambda *a, **kw: io.BytesIO(
        b'{"size":"7","generation":"1","md5Hash":"wrong"}'))
    with pytest.raises(ValueError, match="checksum"):
        backup.upload_file(path, "backup")


def test_failed_archive_validation_keeps_previous_backup_and_never_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "low_load_backup", nullcontext)
    monkeypatch.setattr(backup, "DATA", tmp_path)
    (tmp_path / "backup.dump").write_bytes(b"previous complete backup")
    def run(command, **kwargs):
        if "pg_dump" in command:
            kwargs["stdout"].write(b"truncated dump")
        else:
            raise backup.subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(backup, "upload_file", lambda *a: pytest.fail("must not upload"))
    monkeypatch.setattr(backup, "publish_backup_health", lambda *a: pytest.fail("must not mark healthy"))
    with pytest.raises(backup.subprocess.CalledProcessError):
        backup.backup()
    assert (tmp_path / "backup.dump").read_bytes() == b"previous complete backup"


def test_success_publishes_health_only_after_archive_validation_and_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "low_load_backup", nullcontext)
    monkeypatch.setattr(backup, "DATA", tmp_path)
    events = []
    def run(command, **kwargs):
        if "pg_dump" in command:
            events.append("dump")
            kwargs["stdout"].write(b"complete archive")
        else:
            assert kwargs["stdin"].read() == b"complete archive"
            assert "--file=/dev/null" in command
            events.append("verify")
    def upload(path, name):
        events.append("upload")
        assert path.read_bytes() == b"complete archive"
        return dict(bytes=path.stat().st_size, generation="1")
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(backup, "upload_file", upload)
    monkeypatch.setattr(backup, "publish_backup_health", lambda state: events.append(
        "healthy" if state["archive_verified"] else "bad"))
    backup.backup()
    assert events == ["dump", "verify", "upload", "healthy"]


def test_backup_timeout_ends_low_load_and_keeps_last_verified_archive(tmp_path, monkeypatch):
    from contextlib import contextmanager
    events = []
    @contextmanager
    def mode():
        events.append("paused")
        try:
            yield
        finally:
            events.append("resumed")
    monkeypatch.setattr(backup, "DATA", tmp_path)
    monkeypatch.setattr(backup, "low_load_backup", mode)
    (tmp_path / "backup.dump").write_bytes(b"good")
    def run(command, **kwargs):
        if "psql" in command:
            assert "application_name = 'jobradar-online-backup'" in command[-1]
            events.append("cancelled")
            return
        assert command.index("timeout") > command.index("postgres")
        assert "--kill-after=10s" in command
        raise backup.subprocess.TimeoutExpired(command, 5420)
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(backup, "upload_file", lambda *a: pytest.fail("no upload after failure"))
    with pytest.raises(backup.subprocess.TimeoutExpired):
        backup.backup()
    assert events == ["paused", "cancelled", "resumed"]
    assert (tmp_path / "backup.dump").read_bytes() == b"good"


def test_backup_resumes_heavy_work_before_network_upload(tmp_path, monkeypatch):
    from contextlib import contextmanager
    state = {"paused": False}
    @contextmanager
    def mode():
        state["paused"] = True
        try:
            yield
        finally:
            state["paused"] = False
    monkeypatch.setattr(backup, "DATA", tmp_path)
    monkeypatch.setattr(backup, "low_load_backup", mode)
    def run(command, **kwargs):
        assert state["paused"]
        if "pg_dump" in command:
            kwargs["stdout"].write(b"archive")
    def upload(path, name):
        assert not state["paused"]
        return dict(bytes=7)
    monkeypatch.setattr(backup.subprocess, "run", run)
    monkeypatch.setattr(backup, "upload_file", upload)
    monkeypatch.setattr(backup, "publish_backup_health", lambda state: None)
    backup.backup()
