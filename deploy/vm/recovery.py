#!/usr/bin/env python3
"""Host-side maintenance leases, verified recovery and bounded local repair.

Installed beside runtime_control.py. No network calls, new cloud resources or
automatic database restarts. A one-minute systemd timer runs `check`.
"""
import fcntl
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

try:
    from runtime_control import atomic_json, maintenance, read_json, status_ok
except ModuleNotFoundError:  # repository tests
    from jobradar.runtime import atomic_json, maintenance, read_json, status_ok

CONTROL = Path("/run/jobradar")
ROOT = Path("/opt/jobradar")
DATA = Path("/var/lib/jobradar")


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, timeout=30, **kwargs)


def worker_status():
    return read_json(CONTROL / "status/worker.json")


def listener_ok():
    value = read_json(CONTROL / "status/listener.json")
    age = time.time() - value.get("checked_at", 0)
    return 0 <= age < 90 and value.get("state") == "running"


def verify(*, worker=True, listener=True, timeout=120, after=0):
    deadline = time.monotonic() + timeout
    while True:
        state = worker_status()
        worker_ok = (status_ok(state) and state.get("checked_at", 0) >= after
                     and not state.get("collection_stalled")
                     and state.get("match_fail_streak", 0) < 3
                     and state.get("notify_fail_streak", 0) < 3
                     and all(s.get("state") not in {"starting", "error"} for s in state.get("stages", {}).values()))
        listen = read_json(CONTROL / "status/listener.json")
        if ((not worker or worker_ok) and
                (not listener or (listener_ok() and not listen.get("error")
                                  and listen.get("checked_at", 0) >= after))):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("recovery verification failed: no fresh healthy worker/listener status")
        time.sleep(2)


@contextmanager
def low_load_backup(timeout=600):
    """Ask running workers to drain heavy work; never stop serving Telegram."""
    if maintenance(CONTROL):
        raise RuntimeError("another maintenance lease is active")
    now, token = time.time(), uuid.uuid4().hex
    request = dict(kind="backup", token=token, started_at=now, expires_at=now + 3 * 3600)
    atomic_json(CONTROL / "maintenance.json", request)
    try:
        deadline = time.monotonic() + timeout
        while True:
            state = worker_status()
            if (status_ok(state) and state.get("quiesced")
                    and state.get("maintenance", {}).get("token") == token):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("backup drain timed out; backup aborted, workers remain running")
            time.sleep(2)
        yield
    finally:
        if read_json(CONTROL / "maintenance.json").get("token") == token:
            (CONTROL / "maintenance.json").unlink(missing_ok=True)


def allow_repair(now):
    path = DATA / "recovery-attempts.json"
    attempts = [t for t in read_json(path).get("attempts", []) if 0 <= now - t < 1800]
    if len(attempts) >= 3:
        raise RuntimeError("recovery limited to three attempts per 30 minutes; manual attention required")
    atomic_json(path, {"attempts": attempts + [now]})


def check():
    # A repair cannot race a deploy/compaction/backup. Never wait behind them.
    with (DATA / "operations.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if maintenance(CONTROL):
            return
        # Explicit rollback/administrative disable must remain respected.
        enabled = subprocess.run(["systemctl", "is-enabled", "--quiet", "jobradar-workers"], timeout=10).returncode == 0
        if not enabled:
            return
        state = worker_status()
        repair_worker = not status_ok(state) or state.get("collection_stalled", False)
        repair_listener = not listener_ok()
        if not repair_worker and not repair_listener:
            return
        # Give startup its full verification window before counting a failure.
        active = subprocess.run(["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic", "--value",
                                 "jobradar-workers"], capture_output=True, text=True, timeout=10)
        since = int(active.stdout.strip() or "0") / 1_000_000
        if since and time.monotonic() - since < 180:
            return
        now = time.time()
        allow_repair(now)
        if repair_worker:
            run("systemctl", "reset-failed", "jobradar-workers")
            # Starting/restarting can take the stop grace period; enqueue it and
            # check fresh application status, not just systemctl's return code.
            run("systemctl", "restart", "--no-block", "jobradar-workers")
        if repair_listener:
            run("docker", "compose", "up", "-d", "--no-deps", "listener")
            run("docker", "compose", "restart", "--no-deps", "listener")
        verify(worker=repair_worker, listener=repair_listener, timeout=240, after=now)
        print("Recovery verified with fresh application health")


if __name__ == "__main__":
    try:
        if sys.argv[1] == "verify":
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--worker", type=int, default=1, choices=[0, 1])
            parser.add_argument("--listener", type=int, default=1, choices=[0, 1])
            parser.add_argument("--after", type=float, default=0)
            args = parser.parse_args(sys.argv[2:])
            verify(worker=bool(args.worker), listener=bool(args.listener), after=args.after)
        elif sys.argv[1] == "check":
            check()
        else:
            raise ValueError("expected check or verify")
    except Exception as exc:
        print(f"JobRadar recovery failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
