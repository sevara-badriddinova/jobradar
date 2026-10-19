# Continuous workers — October 19, 2026

The legacy `cycle` ran every stage serially every five minutes: collect 50
boards, filter, match 25, then notify (which re-ran filtering and application
linking). A cycle took ~12 minutes, so 12,000+ boards were each revisited only
about every two days — most of the 72-hour window was spent waiting for a scan.

`jobradar worker` replaces the timer with one long-running process whose stages
communicate only through PostgreSQL:

```
scheduler ─► collectors (4 concurrent for the trial, per-provider caps + rate limits, byte budget)
                 │  ingest + queue filter tasks for new/changed jobs (one transaction)
                 ▼
            work_queue(filter) ─► filter worker ─► work_queue(match) ─► 2 match workers
                                                                            │ (daily AI cap)
                                         notifications outbox ◄─────────────┘
                                                 │
                                         Telegram outbox worker
```

No Kafka, Redis, or Kubernetes: one container, threads, and PostgreSQL rows.

## Durable queues (migration 0010, additive)

- `board_state` — one row per registry board: `next_due_at`, lease, failure
  backoff, last payload size. Collectors claim the most overdue boards per
  provider with `FOR UPDATE SKIP LOCKED`. Healthy boards revisit every
  30 min × priority; transient failures back off 10 min → 6 h; permanent 4xx
  (404/403/410…) 6 h → 7 days, so dead addresses stop consuming capacity.
- `work_queue` — filter/match tasks, unique per `(kind, job)`. Claims lease rows
  atomically; a keeper thread extends leases of in-flight work every minute, so
  only crashed work expires (5 minutes) and is reclaimed by any worker.
  `generation` re-runs a task re-enqueued while it was leased. Failures retry
  with jittered exponential backoff; tasks become `dead` after `max_attempts`
  (filter 5, match 12 ≈ a day of LLM outage) and an explicit requeue revives them.
- `notifications.origin_at` — when ingestion saw the job new/changed, for the
  discovery-to-alert latency metric.

## What is preserved

- Only new, changed, or explicitly requeued jobs are processed: ingestion
  clears `filtered_at` exactly as before and queues those rows in the same
  transaction. Unchanged polls queue nothing and write no `collector_runs` row
  (failures and polls that changed something are still recorded).
- Filtering and matching call the legacy `run_filters` / `run_matching`
  restricted to the claimed job ids, so the 72-hour cutoff, role-specific
  sponsorship evidence, experience rules, the match cache, and application
  history behave identically. A filter-policy change still forces one full
  recheck. A maintenance sweep runs the legacy incremental filter every 10
  minutes (moving 72-hour window and a safety net for any untasked job).
- Applications are linked every 2 minutes and before each digest; send-time
  eligibility (`closed`, status, review choice, age, sponsorship) is unchanged.
- Telegram deduplication: the existing outbox (one row per job/channel,
  claim-before-send, uncertain "sending" never resent automatically).
- The cycle advisory lock is held by the worker for its lifetime, so the
  legacy cycle cannot run concurrently.

## Backpressure and resource bounds

- Collection pauses while more than 20,000 filter tasks are queued. Matching
  (the paid, slow stage) never blocks discovery; it claims newest jobs first.
- Every collection streams responses under a shared 64 MB byte budget,
  reserving its previous payload size up front. A response larger than the
  whole budget (e.g. the 78 MB DevFinders board) runs alone; a single response
  over 128 MB fails without retry. Waiting more than 30 s for budget
  reschedules the board in a minute without counting a failure.
- Token buckets per provider host family (Workday 2 rps, Greenhouse 4 rps,
  Ashby/Lever/Recruitee 2 rps, others 1 rps) plus per-provider concurrency caps.
- Database pool: concurrency + 4 connections (PostgreSQL allows 30).

## Daily AI spending cap

Every model call is metered into `ai_usage` (UTC day × model: calls, input and
output tokens, cost at the published per-token rates in `llm.PRICES`; unknown
models are costed at the most expensive listed rate). Set
`JOBRADAR_AI_DAILY_BUDGET_USD` (or `jobradar worker --ai-daily-budget-usd`) to cap
spend: once today's recorded cost reaches it, match workers stop claiming, and a
batch interrupted by the cap is released without charging an attempt. Queued
jobs are preserved and resume after the next UTC midnight (still subject to the
72-hour cutoff at send time). The legacy `match` command records spend and
honours the same variable. One in-flight call per match thread may overrun the
cap by its own cost (about $0.01 with Claude Haiku 4.5).

## Measured under the VM's limits

`tests/load/vm_limits.py` runs the real worker in a 512 MiB memory cgroup with
PostgreSQL (64 MB shared buffers, 2 MB work_mem) in a 256 MiB cgroup, against a
local Greenhouse-shaped ATS: 1,500 boards × 25 postings, 50 dead (404) boards,
20 slow boards, and one 78 MB board (94 postings).

| Measure | Result |
|---|---|
| New-content collection | 302–372 boards/min (all 1,571 boards in ~5 min) |
| Unchanged re-poll | 608–750 boards/min, 0 filter tasks queued |
| Worker peak anonymous RSS / cgroup | 317 MiB / 386 MiB of 512 MiB, failcnt 0 |
| PostgreSQL peak anonymous RSS | 71 MiB (rest of 256 MiB is reclaimable cache), 0 OOM kills |
| Byte budget peak | 64 MB (the 78 MB board ran alone) |
| Dead boards | 50 backed off ≥ 6 h after one failure |

The synthetic pass rate (≈60%) is far above production's (≈1%), so the match
backlog it builds is a stress case; real throughput is bounded by provider
rate limits rather than CPU or memory.

## Operations

```bash
jobradar worker                     # run until SIGTERM (--duration N for a trial)
jobradar metrics                    # queue depth/age, board coverage, failures, alert latency
jobradar requeue filter|match [ids] # explicit reprocessing (all pending when no ids)
jobradar requeue boards [names]     # make boards due now
```

On the VM (`deploy/vm/install-workers.sh`, then `/opt/jobradar/workers.sh`):

- `trial` stops the poll timer (still enabled for reboot) after any running
  cycle finishes and starts the workers without enabling them.
- `enable` cuts over permanently; `rollback` restores the poll timer.
- The worker holds the maintenance lock in shared mode: the legacy poll skips,
  maintenance scripts wait, and the backup unit's drop-in stops the workers
  before dumping and resumes them afterwards if they were running.
- Metrics are logged as one JSON line per minute (`journalctl -u
  jobradar-workers`) and stored in `app_state.worker_metrics`; the cloud
  heartbeat is written every 5 minutes while boards succeed and the matcher
  and outbox are not failing repeatedly.
