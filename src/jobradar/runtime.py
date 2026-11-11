"""Small, local-only runtime controls shared by the worker and VM scripts.

No database, cloud calls or credentials. Maintenance requests are leases, never
permanent stop switches. Atomic files let host recovery inspect a stuck worker.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

MAX_MAINTENANCE_SECONDS = 3 * 3600


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path: Path, value: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(value, out, allow_nan=False)
            out.flush()
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def maintenance(path: Path | None, *, now: float | None = None) -> dict:
    if path is None:
        return {}
    value = read_json(path / "maintenance.json")
    now = time.time() if now is None else now
    try:
        start, end = float(value["started_at"]), float(value["expires_at"])
        valid = (math.isfinite(start) and math.isfinite(end)
                 and start <= now < end and 0 < end - start <= MAX_MAINTENANCE_SECONDS
                 and value["kind"] in {"backup", "compact", "deploy"}
                 and isinstance(value["token"], str) and bool(value["token"]))
        return value if valid else {}
    except (KeyError, ValueError, TypeError):
        return {}


def status_ok(value: dict, *, now: float | None = None, max_age: float = 45) -> bool:
    now = time.time() if now is None else now
    try:
        age = now - float(value["checked_at"])
        stages = value["stages"]
        return (0 <= age <= max_age and value["state"] == "running"
                and isinstance(stages, dict) and bool(stages)
                and all(isinstance(s, dict) and s.get("alive") is True and s.get("stalled") is False
                        for s in stages.values()))
    except (KeyError, ValueError, TypeError):
        return False


def cleanup_permitted(path: Path | None, *, now: float | None = None) -> bool:
    """Only prune after a recent, verified, uploaded backup; fail closed."""
    if path is None:
        return False
    value = read_json(path / "backup.json")
    try:
        now = time.time() if now is None else now
        age = now - datetime.fromisoformat(value["completed_at"].replace("Z", "+00:00")).timestamp()
        return value["archive_verified"] is True and int(value["bytes"]) > 0 and 0 <= age < 36 * 3600
    except (KeyError, TypeError, ValueError):
        return False
