"""One bounded polling cycle, suitable for Cloud Run Jobs."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)
LOCK_ID = 0x4A4F425241444152  # stable signed bigint: JOBRADAR


@contextmanager
def cycle_lock(engine: Engine) -> Iterator[bool]:
    """Keep a dedicated direct PostgreSQL session for the whole cycle.

    Invalidate on exit so even exceptional paths cannot return a session-level
    lock to the connection pool. The server releases it when the session ends.
    Manual one-shot CLI commands do not participate in this lock.
    """
    if "-pooler" in (engine.url.host or ""):
        raise ValueError("cycle requires a direct Neon connection (disable Connection pooling)")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        try:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_ID}
            ).scalar_one()
            yield acquired
        finally:
            conn.invalidate()


def run_cycle(args, run: Callable[[list[str]], int]) -> int:
    """Continue after individual board failures; stop on prerequisite failures."""
    def stage(*argv: str) -> int:
        log.info("cycle stage started: %s", " ".join(argv))
        result = run(["--profile", args.profile, *argv])
        log.info("cycle stage finished: %s (exit=%s)", argv[0], result)
        return result

    collect_status = stage("collect")
    if collect_status not in (0, 1):
        return collect_status
    if args.applications != "none":
        status = stage("applications", "sync", "--source", args.applications,
                       "--limit", str(args.inbox_limit))
        if status:
            log.error("application sync failed; holding matching and notifications")
            return status
    status = stage("filter")
    if status:
        return status
    match_status = 0
    if args.match:
        match_status = stage("match", "--limit", str(args.match_limit))
        if match_status not in (0, 1):
            return match_status
    if args.notify:
        status = stage("notify", *(["--digest"] if args.digest else []))
        if status:
            return status
    return max(collect_status, match_status)
