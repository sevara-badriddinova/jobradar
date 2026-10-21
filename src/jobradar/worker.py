"""Continuous multi-worker pipeline in one process (sized for a 1 GB VM).

    scheduler -> concurrent ATS collectors -> PostgreSQL
              -> filter worker -> AI matching worker -> Telegram outbox worker

Stages communicate only through PostgreSQL (board_state, work_queue, and the
existing notifications outbox), so a slow board scan never delays the filter,
match, or alert of a job another board just produced, and any thread or the
whole process can crash without losing or duplicating work:

- every claim is `FOR UPDATE SKIP LOCKED` with a lease; a keeper thread extends
  leases of in-flight work, and expired leases are reclaimed by any worker;
- handlers are idempotent (ingestion, filters, the match cache, and the outbox's
  one-row-per-job identity with claim-before-send, where an uncertain delivery
  is never resent automatically);
- only new/changed jobs are queued (ingestion clears `filtered_at`), plus
  explicit requeues and a periodic safety sweep identical to the legacy filter.

Filtering, matching, and delivery reuse the legacy functions restricted to the
claimed job ids, so the 72-hour cutoff, role-specific sponsorship rules,
application history, and existing filter behaviour are unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine

from jobradar import llm
from jobradar import queue as q
from jobradar.ai_budget import DailyBudgetMeter
from jobradar.applications import link_applications
from jobradar.candidate import refresh_experience
from jobradar.collectors import build_collector
from jobradar.db import notifications, work_queue
from jobradar.filters import run_filters
from jobradar.http import BackpressureError, ByteBudget, RateLimiter, byte_bounded, make_client
from jobradar.matcher import invalidate_stale_matches, run_matching
from jobradar.notifications import enqueue as enqueue_notifications
from jobradar.notifications import send_digest, send_immediate
from jobradar.pipeline import run_collector
from jobradar.settings import Settings

log = logging.getLogger(__name__)

ALL_ROLES = frozenset({"schedule", "collect", "filter", "match", "notify"})
MB = 1024 * 1024


@dataclass
class WorkerConfig:
    roles: frozenset[str] = ALL_ROLES
    collect_concurrency: int = 4             # trial setting for the 1 GB VM
    provider_concurrency: dict[str, int] = field(default_factory=lambda: {
        "greenhouse": 3, "workday": 2, "ashby": 2, "lever": 2})
    default_provider_concurrency: int = 2
    # Healthy board revisit (x priority). 80 min x priority 3 = every 4 h:
    # 12k priority-3 boards need ~51 boards/min, inside the 1 GB VM's measured
    # 50-100/min. The old 30 min (136 boards/min) left a permanent backlog.
    base_interval: float = 4800.0
    byte_budget: int = 64 * MB             # response bytes held by all collections
    max_response: int = 128 * MB           # a single response larger than this fails
    byte_wait: float = 30.0
    board_lease: float = 300.0
    task_lease: float = 300.0
    filter_batch: int = 200
    match_batch: int = 4
    match_max_attempts: int = 12           # ~1 day of backoff during an LLM outage
    max_filter_backlog: int = 20000        # backpressure: pause collection above this
    max_match_backlog: int | None = None   # matching never blocks discovery by default
    match_concurrency: int = 2
    ai_daily_budget_usd: float | None = None  # None: record spend, never pause
    digest_interval: float = 900.0
    idle_sleep: float = 5.0
    maintenance_interval: float = 30.0
    link_interval: float = 120.0
    sweep_interval: float = 3600.0         # belt and braces: every new job already has a task;
                                           # delivery re-checks the 72 h window at send time
    seed_interval: float = 600.0
    discover_interval: float = 3600.0
    metrics_interval: float = 60.0
    heartbeat_interval: float = 300.0          # watchdog alerts after 30 stale minutes
    keeper_interval: float = 60.0
    due_cache_seconds: float = 10.0
    shutdown_timeout: float = 90.0


class Worker:
    def __init__(self, engine: Engine, config: WorkerConfig, *,
                 settings_loader: Callable[[], Settings],
                 telegram: tuple | None = None,                 # (Telegram, chat_id)
                 collector_factory=build_collector,
                 http_client=None,
                 profile_path: str = "profile.toml",
                 discover: Callable[[], object] | None = None,
                 heartbeat: Callable[[], object] | None = None) -> None:
        self.engine, self.cfg = engine, config
        self._load_settings = settings_loader
        self.settings = settings_loader()
        self.telegram = telegram
        self.collector_factory = collector_factory
        self.limiter = RateLimiter()
        self.http = http_client or make_client()
        self.http.event_hooks.setdefault("request", []).append(self.limiter.hook)
        self.budget = ByteBudget(config.byte_budget)
        self.meter = DailyBudgetMeter(engine, config.ai_daily_budget_usd)
        llm.set_meter(self.meter)
        self._budget_recheck = 0.0
        self.profile_path = profile_path
        self.discover, self.heartbeat = discover, heartbeat
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop = threading.Event()
        self.wake = {k: threading.Event() for k in ("filter", "match", "notify", "slot")}
        self.counters: Counter = Counter()
        self._lock = threading.Lock()
        self._profile_lock = threading.Lock()
        self._boards: dict[int, q.Board] = {}         # in flight, for the lease keeper
        self._tasks: dict[int, q.Task] = {}
        self._inflight: Counter = Counter()           # provider -> running collections
        self._profile = None
        self._last: dict[str, float] = {}
        self._last_board_ok = time.monotonic()
        self._match_fail_streak = 0
        self._notify_fail_streak = 0
        self._backlog = (0, 0, 0.0)                   # filter, match, measured at
        self._due_cache: tuple[dict[str, tuple[int, float]], float] = ({}, -1e18)
        self._claim_seq = 0                    # fair-claim rotation state
        self._last_served: dict[str, int] = {}

    # --- small helpers ----------------------------------------------------------------
    def count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counters[key] += n

    def due(self, name: str, interval: float) -> bool:
        now = time.monotonic()
        if now - self._last.get(name, -1e18) >= interval:
            self._last[name] = now
            return True
        return False

    @property
    def needs_sponsorship(self) -> bool:
        return self.settings.preferences.needs_visa_sponsorship is True

    def backlog(self) -> tuple[int, int]:
        filter_n, match_n, at = self._backlog
        if time.monotonic() - at > 5:
            with self.engine.connect() as conn:
                rows = dict(conn.execute(select(work_queue.c.kind, func.count()).where(
                    work_queue.c.state != "dead").group_by(work_queue.c.kind)).all())
            filter_n, match_n = rows.get("filter", 0), rows.get("match", 0)
            self._backlog = (filter_n, match_n, time.monotonic())
        return filter_n, match_n

    # --- collection ---------------------------------------------------------------------
    def provider_limit(self, provider: str) -> int:
        return self.cfg.provider_concurrency.get(provider, self.cfg.default_provider_concurrency)

    def claim_boards(self, free: int) -> list[q.Board]:
        """Claim up to `free` overdue boards, round-robin across providers that
        still have capacity, most overdue first within each provider."""
        if free <= 0:
            return []
        # The per-provider due count only orders providers (the claim itself
        # re-checks due/lease), so a few seconds of staleness is harmless. It
        # aggregates the whole schedule (~260 ms on the VM): don't run it per claim.
        due, at = self._due_cache
        fresh = time.monotonic() - at > self.cfg.due_cache_seconds
        if fresh:
            with self.engine.begin() as conn:
                due = q.due_overview(conn)
            self._due_cache = (due, time.monotonic())
        claimed = self._claim_from(due, free)
        if not claimed and not fresh and self._spare_capacity(due):
            # Nothing came from the cached providers although slots are free:
            # the cached view may be stale (e.g. other providers became due).
            with self.engine.begin() as conn:
                due = q.due_overview(conn)
            self._due_cache = (due, time.monotonic())
            claimed = self._claim_from(due, free)
        return claimed

    def _spare_capacity(self, due: dict) -> bool:
        """True unless every cached due provider is already at its concurrency cap."""
        return not due or any(self._inflight[p] < self.provider_limit(p) for p in due)

    def _claim_from(self, due: dict[str, tuple[int, float]], free: int) -> list[q.Board]:
        """Fair claiming across providers, one board per slot.

        Even slots rotate: they go to the due provider served least recently,
        so every provider with due boards is polled within one rotation
        (providers / 2 slots... claims) however large the other backlogs are.
        Odd slots go to the provider holding the most overdue board, which
        keeps big providers served in proportion to their backlog. The old
        "largest backlog takes every slot" rule starved small providers.
        """
        claimed: list[q.Board] = []
        exhausted: set[str] = set()
        while len(claimed) < free:
            open_ = [p for p in due if p not in exhausted
                     and self._inflight[p] < self.provider_limit(p)]
            if not open_:
                break
            if self._claim_seq % 2 == 0:
                provider = min(open_, key=lambda p: (self._last_served.get(p, -1), due[p][0], due[p][1], p))
            else:
                provider = min(open_, key=lambda p: (due[p][1], p))
            with self.engine.begin() as conn:
                boards = q.claim_boards(conn, provider, self.owner, 1, self.cfg.board_lease)
            if not boards:
                exhausted.add(provider)
                continue
            self._claim_seq += 1
            self._last_served[provider] = self._claim_seq
            claimed.extend(boards)
            for b in boards:
                with self._lock:
                    self._inflight[b.collector] += 1
                    self._boards[b.company_id] = b
        return claimed

    def collect_board(self, board: q.Board) -> str:
        """Poll one board; returns ok | failed | backpressure | lost."""
        def queue_filters(conn, report):
            report.queued = q.enqueue_pending_filters(conn, board.collector, board.board)

        report, nbytes, outcome, pressured, error = None, None, "failed", False, None
        try:
            reserve = int((board.last_bytes or 0) * 1.25)
            with byte_bounded(self.budget, max_response=self.cfg.max_response, reserve=reserve,
                              wait=self.cfg.byte_wait) as meter:
                collector = self.collector_factory(board.collector, board.board, company=board.name,
                                                   client=self.http)
                report = run_collector(self.engine, collector, after_ingest=queue_filters,
                                       record_unchanged=False,
                                       ingest_max_age_days=self.settings.ingestion.max_age_days,
                                       known_snapshot=board.snapshot_hash)
                nbytes = meter.total
                pressured = meter.backpressure
        except BackpressureError:
            pressured = True
        except Exception as exc:  # boundary: a factory/collector bug must not kill the pool
            log.exception("collector %s/%s crashed", board.collector, board.board)
            report, error = None, f"{type(exc).__name__}: {exc}"
        try:
            with self.engine.begin() as conn:
                if pressured:
                    # Budget ran out mid-collection: anything ingested was a
                    # partial snapshot (never closes jobs); retry the whole board soon.
                    kept = q.finish_board(conn, board, self.owner, ok=False, error=None, fetched=None,
                                          nbytes=None, retry_in=60)
                    outcome = "backpressure"
                elif report is None:
                    kept = q.finish_board(conn, board, self.owner, ok=False, error=error, fetched=None,
                                          nbytes=None, base_interval=self.cfg.base_interval)
                else:
                    kept = q.finish_board(conn, board, self.owner, ok=report.ok, error=report.error,
                                          fetched=report.fetched, nbytes=nbytes,
                                          base_interval=self.cfg.base_interval,
                                          snapshot_hash=report.snapshot_hash)
                    outcome = "ok" if report.ok else "failed"
            if not kept:
                outcome = "lost"
        finally:
            with self._lock:
                self._inflight[board.collector] -= 1
                self._boards.pop(board.company_id, None)
            self.wake["slot"].set()   # a provider slot opened: claim the next board now
        self.count(f"collect_{outcome}")
        if report is not None and report.unchanged:
            self.count("collect_unchanged_skipped")
        if report is not None and report.result is not None and report.result.skipped_old:
            self.count("ingest_skipped_old", report.result.skipped_old)
        if outcome == "ok":
            self._last_board_ok = time.monotonic()
            if getattr(report, "queued", 0):
                self.count("filter_tasks_queued", report.queued)
                self.wake["filter"].set()
        return outcome

    def collect_loop(self) -> None:
        pool = ThreadPoolExecutor(self.cfg.collect_concurrency, thread_name_prefix="collect")
        running: set[Future] = set()
        try:
            while not self.stop.is_set():
                running = {f for f in running if not f.done()}
                filter_n, match_n = self.backlog()
                if filter_n > self.cfg.max_filter_backlog or (
                        self.cfg.max_match_backlog is not None and match_n > self.cfg.max_match_backlog):
                    self.count("collect_backpressure_pauses")
                    self.stop.wait(self.cfg.idle_sleep)
                    continue
                free = self.cfg.collect_concurrency - len(running)
                self.wake["slot"].clear()
                boards = self.claim_boards(free) if free > 0 else []
                for b in boards:
                    running.add(pool.submit(self.collect_board, b))
                if not boards:
                    # Nothing claimable: all slots/providers busy or nothing due.
                    # A finishing collection wakes us immediately.
                    self.wake["slot"].wait(self.cfg.idle_sleep)
        finally:
            pool.shutdown(wait=True, cancel_futures=False)

    # --- filtering ----------------------------------------------------------------------
    def filter_once(self) -> int:
        with self.engine.begin() as conn:
            tasks = q.claim(conn, "filter", self.owner, self.cfg.filter_batch, self.cfg.task_lease)
        if not tasks:
            return 0
        try:
            self._filter(tasks)
        except Exception as exc:
            log.exception("filter batch of %d failed; isolating", len(tasks))
            if len(tasks) == 1:
                with self.engine.begin() as conn:
                    q.fail(conn, tasks[0], self.owner, f"{type(exc).__name__}: {exc}")
                self.count("filter_failed")
            else:
                for task in tasks:  # isolate a poison job instead of failing the batch
                    try:
                        self._filter([task])
                    except Exception as one:
                        with self.engine.begin() as conn:
                            q.fail(conn, task, self.owner, f"{type(one).__name__}: {one}")
                        self.count("filter_failed")
        return len(tasks)

    def _filter(self, tasks: list[q.Task]) -> None:
        settings = self.settings
        with self.engine.begin() as conn:
            result = run_filters(conn, settings.filters, needs_sponsorship=self.needs_sponsorship,
                                 only_ids=[t.job_id for t in tasks])
            origin = {t.job_id: t.origin_at for t in tasks}
            passed = result.passed_ids
            for origin_at in sorted({origin.get(i) for i in passed}, key=lambda x: (x is None, x)):
                ids = [i for i in passed if origin.get(i) == origin_at]
                q.enqueue(conn, "match", ids, origin_at=origin_at, max_attempts=self.cfg.match_max_attempts)
            for task in tasks:
                q.complete(conn, task, self.owner)
        self.count("filter_done", len(tasks))
        if passed:
            self.count("match_tasks_queued", len(passed))
            self.wake["match"].set()

    # --- matching -----------------------------------------------------------------------
    def profile(self):
        with self._profile_lock:  # two match threads must not both version it
            if self._profile is None or self.due("profile", 300):
                with self.engine.begin() as conn:
                    self._profile = refresh_experience(conn)
            return self._profile

    def budget_paused(self) -> bool:
        """True while today's AI cap is reached. Tasks stay queued, untouched."""
        if self.cfg.ai_daily_budget_usd is None:
            return False
        if time.monotonic() < self._budget_recheck:
            return True
        if self.meter.exhausted():
            self._budget_recheck = time.monotonic() + 300
            self.count("match_budget_pauses")
            log.warning("daily AI budget of $%.2f reached; matching paused, tasks kept queued",
                        self.cfg.ai_daily_budget_usd)
            return True
        return False

    def match_once(self) -> int:
        if self.budget_paused():
            return 0
        with self.engine.begin() as conn:
            tasks = q.claim(conn, "match", self.owner, self.cfg.match_batch, self.cfg.task_lease)
        if not tasks:
            return 0
        profile = self.profile()
        if profile is None:
            with self.engine.begin() as conn:
                q.release(conn, tasks, self.owner, delay=600)
            log.warning("no candidate profile; matching paused")
            return 0
        with self._lock:
            self._tasks.update((t.id, t) for t in tasks)
        try:
            settings = self.settings
            result = run_matching(self.engine, profile, settings.preferences, limit=len(tasks),
                                  max_posting_age_days=settings.filters.max_posting_age_days,
                                  job_ids=[t.job_id for t in tasks], invalidate=False)
        except llm.BudgetExceeded as exc:
            # Keep every claimed task (no attempt charged). Jobs assessed before
            # the cap was hit are cache hits when they are retried.
            with self.engine.begin() as conn:
                q.release(conn, tasks, self.owner, delay=300)
            self._budget_recheck = time.monotonic() + 300
            self.count("match_budget_pauses")
            log.warning("%s; %d match task(s) kept queued", exc, len(tasks))
            return 0
        except Exception as exc:
            log.exception("match batch crashed")
            with self.engine.begin() as conn:
                for t in tasks:
                    q.fail(conn, t, self.owner, f"{type(exc).__name__}: {exc}")
            self.count("match_failed", len(tasks))
            self._match_fail_streak += 1
            return len(tasks)
        finally:
            with self._lock:
                for t in tasks:
                    self._tasks.pop(t.id, None)
        failed, attempted = set(result.failed_ids), set(result.attempted_ids)
        with self.engine.begin() as conn:
            for t in tasks:
                if t.job_id in failed:
                    q.fail(conn, t, self.owner, "LLM assessment failed")
                elif result.aborted and t.job_id not in attempted:
                    q.release(conn, [t], self.owner, delay=120)
                else:  # assessed, cached, or no longer eligible: nothing left to do
                    q.complete(conn, t, self.owner)
            queued = self._queue_notifications(conn, tasks)
        self.count("match_done", len(tasks) - len(failed))
        self.count("match_failed", len(failed))
        self._match_fail_streak = self._match_fail_streak + 1 if failed and len(failed) == len(attempted) else 0
        if queued:
            self.wake["notify"].set()
        return len(tasks)

    def _queue_notifications(self, conn, tasks: list[q.Task] = ()) -> int:
        settings = self.settings
        queued = enqueue_notifications(conn, settings.notifications,
                                       max_posting_age_days=settings.filters.max_posting_age_days)
        for t in tasks:
            conn.execute(update(notifications).where(notifications.c.job_id == t.job_id,
                notifications.c.origin_at.is_(None)).values(origin_at=t.origin_at))
        return queued["immediate"] + queued["digest"]

    # --- notifications ------------------------------------------------------------------
    def notify_once(self, *, digest: bool = False) -> int:
        if self.telegram is None:
            return 0
        tg, chat_id = self.telegram
        settings = self.settings
        if settings.notifications.delivery != "alerts":
            # Queue (Mini App) delivery: the exact reconciliation `notify` does,
            # publishing one queue update instead of per-job alerts.
            from jobradar.cli import deliver_queue_mode
            queued = deliver_queue_mode(self.engine, tg, chat_id, settings)
            self.count("queue_updates")
            return queued
        policy = dict(max_posting_age_days=settings.filters.max_posting_age_days,
                      require_explicit_sponsorship=settings.filters.require_explicit_sponsorship)
        sent = send_immediate(self.engine, tg, chat_id, **policy)
        total, failed = sent.sent, sent.failed
        if digest:
            with self.engine.begin() as conn:
                link_applications(conn)  # applied roles are never announced
            d = send_digest(self.engine, tg, chat_id, **policy)
            total, failed = total + d.sent, failed + d.failed
        self.count("notify_sent", total)
        self.count("notify_failed", failed)
        self.count("notify_stuck", sent.stuck)
        self._notify_fail_streak = self._notify_fail_streak + 1 if failed else 0
        return total

    def pending_alerts(self) -> bool:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(notifications).where(
                notifications.c.status == "pending", notifications.c.kind == "immediate")).scalar_one() > 0

    def notify_loop(self) -> None:
        self._last["digest"] = time.monotonic()  # first digest after one interval, like before
        while not self.stop.is_set():
            self.wake["notify"].wait(15)
            self.wake["notify"].clear()
            try:
                if self.settings.notifications.delivery != "alerts":
                    if self.due("legacy_notify", 120):  # the full legacy notify command
                        self.notify_once()
                    continue
                digest = self.due("digest", self.cfg.digest_interval)
                if digest or self.pending_alerts():
                    self.notify_once(digest=digest)
            except Exception:
                log.exception("notification pass failed")
                self.count("notify_errors")
                self.stop.wait(30)

    # --- generic loops ------------------------------------------------------------------
    def stage_loop(self, name: str, step: Callable[[], int]) -> None:
        errors = 0
        while not self.stop.is_set():
            try:
                done = step()
                errors = 0
            except Exception:
                errors += 1
                log.exception("%s loop error", name)
                self.count(f"{name}_errors")
                self.stop.wait(min(300, 5 * 2 ** min(errors, 6)))
                continue
            if not done:
                self.wake[name].wait(self.cfg.idle_sleep)
                self.wake[name].clear()

    def keeper_loop(self) -> None:
        """Extend leases of work still in progress, so only crashed work expires."""
        while not self.stop.wait(self.cfg.keeper_interval):
            self.keep_leases()

    def keep_leases(self) -> None:
        with self._lock:
            boards, tasks = list(self._boards.values()), list(self._tasks.values())
        try:
            with self.engine.begin() as conn:
                for b in boards:
                    q.extend_board(conn, b, self.owner, self.cfg.board_lease)
                q.extend(conn, tasks, self.owner, self.cfg.task_lease)
        except Exception:
            log.exception("lease extension failed")

    # --- maintenance --------------------------------------------------------------------
    def maintain_once(self, *, force: bool = False) -> dict:
        """Periodic safety work; each piece runs on its own interval."""
        done = {}
        with self.engine.begin() as conn:
            done["buried"] = q.bury_exhausted(conn)
        if force or self.due("settings", 60):
            try:
                self.settings = self._load_settings()
            except Exception:
                log.exception("profile settings reload failed; keeping the previous ones")
        if force or self.due("seed", self.cfg.seed_interval):
            with self.engine.begin() as conn:
                done["seeded"] = q.seed_boards(conn)
        if self.discover and (force or self.due("discover", self.cfg.discover_interval)):
            try:
                self.discover()
            except Exception:
                log.exception("board discovery failed")
        if force or self.due("link", self.cfg.link_interval):
            with self.engine.begin() as conn:   # own transaction: commits its progress cursor
                link_applications(conn)
            with self.engine.begin() as conn:
                profile = refresh_experience(conn)
                if profile is not None:
                    invalidate_stale_matches(conn, profile, self.settings.preferences)
                done["notifications_queued"] = self._queue_notifications(conn)
            if done["notifications_queued"]:
                self.wake["notify"].set()
        if force or self.due("sweep", self.cfg.sweep_interval):
            # The legacy incremental pass: the moving 72-hour window, plus any
            # unfiltered job that somehow has no task (belt and braces).
            with self.engine.begin() as conn:
                result = run_filters(conn, self.settings.filters, needs_sponsorship=self.needs_sponsorship)
                q.enqueue(conn, "match", result.passed_ids, max_attempts=self.cfg.match_max_attempts)
            done["swept_passed"] = len(result.passed_ids)
            if result.passed_ids:
                self.wake["match"].set()
        if force or self.due("metrics", self.cfg.metrics_interval):
            done["metrics"] = self.log_metrics()
        if self.heartbeat and (force or self.due("heartbeat", self.cfg.heartbeat_interval)) and self.healthy():
            try:
                self.heartbeat()
            except Exception:
                log.exception("health heartbeat failed")
        return done

    def healthy(self) -> bool:
        collecting = "collect" not in self.cfg.roles or time.monotonic() - self._last_board_ok < 1800
        return collecting and self._match_fail_streak < 3 and self._notify_fail_streak < 3

    def log_metrics(self) -> dict:
        from jobradar.metrics import snapshot
        with self.engine.connect() as conn:
            data = snapshot(conn)
        with self._lock:
            data["ai"] = {"spent_today_usd": round(self.meter.spent(), 4),
                          "daily_budget_usd": self.cfg.ai_daily_budget_usd}
            data["worker"] = dict(self.counters, owner=self.owner, byte_budget_peak=self.budget.peak,
                                  rate_limit_wait_seconds=round(self.limiter.waited, 1))
        log.info("metrics %s", json.dumps(data, default=str, sort_keys=True))
        with self.engine.begin() as conn:
            conn.execute(text("""INSERT INTO app_state (key, value) VALUES ('worker_metrics', :v)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value"""),
                         {"v": json.dumps(data, default=str, sort_keys=True)})
        return data

    def maintenance_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.maintain_once()
            except Exception:
                log.exception("maintenance failed")
                self.count("maintenance_errors")
            self.stop.wait(self.cfg.maintenance_interval)

    # --- lifecycle ----------------------------------------------------------------------
    def run(self, duration: float | None = None) -> None:
        roles = set(self.cfg.roles)
        if "notify" in roles and self.telegram is None:
            log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing; notify role disabled")
            roles.discard("notify")
        threads = [threading.Thread(target=self.keeper_loop, name="keeper", daemon=True)]
        if "schedule" in roles:
            with self.engine.begin() as conn:
                q.seed_boards(conn)
            threads.append(threading.Thread(target=self.maintenance_loop, name="maintenance", daemon=True))
        if "collect" in roles:
            threads.append(threading.Thread(target=self.collect_loop, name="collector", daemon=True))
        if "filter" in roles:
            threads.append(threading.Thread(target=self.stage_loop, args=("filter", self.filter_once),
                                            name="filter", daemon=True))
        if "match" in roles:
            threads += [threading.Thread(target=self.stage_loop, args=("match", self.match_once),
                                         name=f"match-{i}", daemon=True) for i in range(self.cfg.match_concurrency)]
        if "notify" in roles:
            threads.append(threading.Thread(target=self.notify_loop, name="notify", daemon=True))
        log.info("worker %s starting roles=%s", self.owner, sorted(roles))
        for t in threads:
            t.start()
        try:
            self.stop.wait(duration)
        finally:
            self.stop.set()
            for event in self.wake.values():
                event.set()
            # One overall deadline, inside docker's 120 s stop grace period.
            deadline = time.monotonic() + self.cfg.shutdown_timeout
            for t in threads:
                t.join(timeout=max(0.0, deadline - time.monotonic()))
            stuck = [t.name for t in threads if t.is_alive()]
            if stuck:
                log.warning("threads still busy at shutdown (their leases will expire): %s", stuck)
            with self.engine.begin() as conn:
                released = q.release_owner(conn, self.owner)
            log.info("worker %s stopped; released %d lease(s); counters %s",
                     self.owner, released, dict(self.counters))
