import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from urllib.error import URLError

import pytest

spec = importlib.util.spec_from_file_location("vm_cloud_io", Path(__file__).parents[2] / "deploy/vm/cloud_io.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


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
