"""Pipeline metrics read from PostgreSQL (cheap aggregate queries).

Queue depth and age, worker failures, board coverage, and notification latency
(`notifications.sent_at - origin_at`, where origin is when ingestion first saw
the job new or changed).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


def _rows(conn: Connection, sql: str) -> list:
    return conn.execute(text(sql)).mappings().all()


def snapshot(conn: Connection) -> dict:
    queues: dict[str, dict] = {}
    for r in _rows(conn, """
        SELECT kind, state, count(*) AS n,
               extract(epoch FROM now() - min(available_at) FILTER (
                   WHERE state <> 'dead' AND available_at <= now() AND (state = 'ready' OR lease_until < now())
               ))::int AS oldest_ready_s,
               extract(epoch FROM now() - min(origin_at))::int AS oldest_origin_s,
               count(*) FILTER (WHERE attempts > 1) AS retried
        FROM work_queue GROUP BY kind, state"""):
        queues.setdefault(r["kind"], {})[r["state"]] = {
            "n": r["n"], "oldest_ready_s": r["oldest_ready_s"], "oldest_origin_s": r["oldest_origin_s"],
            "retried": r["retried"]}

    boards = _rows(conn, """
        SELECT count(*) AS enabled,
               count(*) FILTER (WHERE b.next_due_at <= now()) AS due,
               count(*) FILTER (WHERE b.lease_until > now()) AS in_flight,
               count(*) FILTER (WHERE b.last_finished_at > now() - interval '1 hour') AS polled_1h,
               count(*) FILTER (WHERE b.last_finished_at > now() - interval '6 hours') AS polled_6h,
               count(*) FILTER (WHERE b.last_finished_at > now() - interval '24 hours') AS polled_24h,
               count(*) FILTER (WHERE b.last_success_at > now() - interval '24 hours') AS ok_24h,
               count(*) FILTER (WHERE b.consecutive_failures > 0) AS failing,
               count(*) FILTER (WHERE b.last_finished_at IS NULL) AS never_polled,
               extract(epoch FROM now() - min(b.next_due_at) FILTER (
                   WHERE b.next_due_at <= now() AND b.next_due_at > '-infinity'))::int AS schedule_lag_s
        FROM board_state b JOIN companies c ON c.id = b.company_id WHERE c.enabled""")[0]

    failures = _rows(conn, """
        SELECT count(*) FILTER (WHERE ok IS FALSE) AS failed_1h,
               count(*) FILTER (WHERE ok) AS changed_ok_1h
        FROM collector_runs WHERE started_at > now() - interval '1 hour'""")[0]

    notes = _rows(conn, """
        SELECT count(*) FILTER (WHERE status = 'pending' AND kind = 'immediate') AS pending_immediate,
               count(*) FILTER (WHERE status = 'pending' AND kind = 'digest') AS pending_digest,
               count(*) FILTER (WHERE status = 'sending') AS uncertain,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'sent' AND sent_at > now() - interval '24 hours') AS sent_24h
        FROM notifications""")[0]
    latency = _rows(conn, """
        SELECT count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM sent_at - origin_at)) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM sent_at - origin_at)) AS p95,
               max(extract(epoch FROM sent_at - origin_at)) AS max
        FROM notifications
        WHERE status = 'sent' AND origin_at IS NOT NULL AND sent_at > now() - interval '24 hours'""")[0]

    def secs(v):
        return None if v is None else round(float(v), 1)

    enabled = boards["enabled"] or 0
    return {
        "queues": queues,
        "boards": {**{k: boards[k] for k in boards},
                   "coverage_24h": round(boards["polled_24h"] / enabled, 4) if enabled else None},
        "collector_runs": dict(failures),
        "notifications": dict(notes),
        "latency_24h_s": {"n": latency["n"], "p50": secs(latency["p50"]), "p95": secs(latency["p95"]),
                          "max": secs(latency["max"])},
    }
