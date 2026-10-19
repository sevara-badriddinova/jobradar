"""Resource test of the continuous worker under the VM's container limits.

Run as root on a disposable PostgreSQL (never production):

    python tests/load/vm_limits.py postgresql://u:p@127.0.0.1:5432/jobradar_load_test 600

It moves PostgreSQL into a 256 MiB memory cgroup and this process into a
512 MiB one (cgroup v1, like the VM's docker mem_limit), starts a local
Greenhouse-shaped ATS, runs the real Worker (collect/filter/match; Telegram
disabled, LLM faked with latency), and prints throughput, memory, OOM and
queue metrics as JSON.
"""
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import insert, text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jobradar import matcher  # noqa: E402
from jobradar.db import candidate_profiles, companies, make_engine, migrate  # noqa: E402
from jobradar.settings import FilterSettings, NotificationSettings, Preferences, Settings  # noqa: E402
from jobradar.worker import Worker, WorkerConfig  # noqa: E402
from tests.factories import FakeLLM, assessment, candidate_profile  # noqa: E402

CG = Path("/sys/fs/cgroup/memory")
PORT = 18765


def cgroup(name, limit_mb, pids):
    path = CG / name
    path.mkdir(exist_ok=True)
    (path / "memory.limit_in_bytes").write_text(str(limit_mb * 1024 * 1024))
    for pid in pids:
        try:
            (path / "cgroup.procs").write_text(str(pid))
        except OSError:
            pass
    return path


def read(path, key):
    return int((path / key).read_text().split()[0])


class Rewrite(httpx.HTTPTransport):
    """Send every ATS request to the local mock, keeping the original host for rate limits."""

    def handle_request(self, request):
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=PORT)
        return super().handle_request(request)


def main(url, seconds, small_boards=1500):
    assert url.rstrip("/").endswith("_test"), "disposable *_test databases only"
    engine = make_engine(url, pool_size=10, max_overflow=2)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    migrate(engine)
    names = ([("big", 1)] + [(f"b{i}", 1) for i in range(small_boards)]
             + [(f"gone{i}", 1) for i in range(50)] + [(f"slow{i}", 1) for i in range(20)])
    with engine.begin() as conn:
        conn.execute(insert(companies), [dict(name=n, collector="greenhouse", board=n, priority=p) for n, p in names])
        conn.execute(insert(candidate_profiles).values(resume_sha256="x", prompt_version="1", model="m",
                     profile=candidate_profile().model_dump(mode="json")))

    pg = cgroup("jr_pg", 256, [int(p) for p in subprocess.run(["pgrep", "-f", "postgres"],
                capture_output=True, text=True).stdout.split()])
    me = cgroup("jr_worker", 512, [os.getpid()])
    for c in (pg, me):
        (c / "memory.max_usage_in_bytes").write_text("0")
    server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mock_ats.py")), str(PORT)])
    time.sleep(1)

    fake = FakeLLM(lambda output, prompt: (time.sleep(0.5),
                   assessment(role_match=9, technical_match=8, experience_match=9))[1])
    matcher.llm.ask = fake
    settings = Settings(filters=FilterSettings(max_posting_age_days=3), preferences=Preferences(interests=["backend"]),
                        notifications=NotificationSettings())
    client = httpx.Client(transport=Rewrite(), timeout=30, follow_redirects=True)
    cfg = WorkerConfig(roles=frozenset({"schedule", "collect", "filter", "match"}), metrics_interval=30,
                       match_batch=4)
    worker = Worker(engine, cfg, settings_loader=lambda: settings, http_client=client)
    worker.limiter.rates["greenhouse.io"] = 20.0      # stress memory/DB, not the real 4 rps
    peaks = {"pg_rss_mb": 0, "worker_rss_mb": 0}
    sampling = [True]

    def sample():
        while sampling[0]:
            for key, path in (("pg_rss_mb", pg), ("worker_rss_mb", me)):
                stat = dict(line.split() for line in (path / "memory.stat").read_text().splitlines())
                peaks[key] = max(peaks[key], round(int(stat["total_rss"]) / 2**20))
            time.sleep(0.25)

    import threading
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.monotonic()
    try:
        worker.run(duration=seconds)
        elapsed = time.monotonic() - started
        # Phase 2: every board due again with unchanged content (steady state).
        with engine.begin() as conn:
            conn.execute(text("""UPDATE board_state SET next_due_at = CASE WHEN last_finished_at IS NOT NULL
                                 THEN now() ELSE now() + interval '1 day' END"""))
            queued_before = conn.execute(text("SELECT count(*) FROM work_queue WHERE kind = 'filter'")).scalar_one()
        repoll = Worker(engine, WorkerConfig(roles=frozenset({"collect", "filter"})),
                        settings_loader=lambda: settings, http_client=client)
        repoll.limiter.rates["greenhouse.io"] = 20.0
        t2 = time.monotonic()
        repoll.run(duration=seconds / 2)
        steady = {"seconds": round(time.monotonic() - t2), "boards_ok": repoll.counters["collect_ok"],
                  "boards_per_min": round(repoll.counters["collect_ok"] / (time.monotonic() - t2) * 60, 1),
                  "filter_tasks_queued": repoll.counters["filter_tasks_queued"], "queued_before": queued_before}
    finally:
        sampling[0] = False
        server.terminate()
    with engine.connect() as conn:
        from jobradar.metrics import snapshot
        metrics = snapshot(conn)
        jobs = conn.execute(text("SELECT status, count(*) FROM jobs GROUP BY 1")).all()
    alive = subprocess.run(["pg_isready", "-h", "127.0.0.1"], capture_output=True).returncode == 0
    report = {
        "seconds": round(elapsed), "boards_ok": worker.counters["collect_ok"],
        "boards_per_min": round(worker.counters["collect_ok"] / elapsed * 60, 1),
        "worker_counters": dict(worker.counters), "byte_budget_peak_mb": round(worker.budget.peak / 2**20, 1),
        "worker_cgroup_peak_mb": round(read(me, "memory.max_usage_in_bytes") / 2**20),
        "worker_rss_peak_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "worker_cgroup_failcnt": read(me, "memory.failcnt"),
        "pg_cgroup_peak_mb": round(read(pg, "memory.max_usage_in_bytes") / 2**20),
        "pg_cgroup_failcnt": read(pg, "memory.failcnt"),
        "pg_oom_kills": dict(line.split() for line in (pg / "memory.oom_control").read_text().splitlines()).get("oom_kill"),
        "postgres_alive": alive, "anonymous_rss_peaks": peaks, "llm_calls": len(fake.calls),
        "jobs_by_status": {s: n for s, n in jobs}, "steady_state_repoll": steady, "metrics": metrics,
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 300)
