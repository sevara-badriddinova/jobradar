# Worker performance on the 1 GB VM — October 20, 2026

The October 20 worker trial on production reached ~14 boards/min with 94% I/O
wait and 444 MB of swap, and its maintenance thread never logged a metric. This
investigation reproduced it on an identical, isolated VM and fixed the causes.

## Method

- **Environment:** temporary `jr-bench`, the same as production: e2-micro (969 MiB RAM,
  2 GB swap, swappiness 10), 30 GB **pd-standard** boot disk, PostgreSQL 16
  (64 MB shared_buffers, 2 MB work_mem, 256 MiB container limit), and a 90 MiB
  stand-in for the Telegram listener.
- **Data:** restored from the 13:14 UTC verified backup (844,555 jobs, 4.25 GB).
  Production's database is 5.0 GB; it has more dead rows.
- **Worker:** the real worker against the real public job-board APIs, roles
  `schedule,collect,filter`. No AI key, no Telegram token, no health bucket.
- **Measurements:** `pg_stat_statements` (`track_io_timing`), `pg_stat_activity`
  wait events every 10 s, table I/O statistics, vmstat/iostat, `/proc/diskstats`,
  docker memory, and py-spy (`--idle`, 10 Hz).
- **Runs:** each run lasted 12–15 minutes. Before each run the board schedule was
  reset and the page cache dropped. The VM was deleted afterwards. Production was
  not changed.

## Why the tests did not predict production

| | Load test (`tests/load/vm_limits.py`) | Production |
|---|---|---|
| Database | small synthetic, fully cached | 4.3–5.0 GB: 0.7–0.8 GB heap, 3.6 GB TOAST, 0.6 GB indexes |
| Disk | local SSD (thousands of IOPS) | 30 GB pd-standard: ~3.6 MB/s, tens of random reads/s |
| Applications | none | 1,375 recorded applications |
| Duplicates/history | none | ~800k historical rows, never-filtered duplicates |

With 64 MB of shared buffers and ~400 MB of page cache, almost every per-job
read on production is a random disk read. The baseline runs saturated the disk
(77–83% busy, 2.3–3.6 MB/s read, the pd-standard ceiling) at 9–14 boards/min,
the same as the production trial. CPU sat idle waiting on I/O.

## Ranked bottlenecks (baseline, 4 collectors, from `pg_stat_statements`)

1. **Full application linking every 2 minutes.** One query (the distinct employer
   keys, then every job at those employers) ran for **718 s of a 12-minute run**,
   608 s of it reading disk. It holds the maintenance thread, which is why
   production never logged metrics. Production's notify digest also ran it
   every 15 minutes.
2. **`UPDATE jobs SET last_seen_at` on every listed job on every poll.** A single
   statement took 13–31 s (for example 265 rows, 6 MB of WAL). Updates are mostly
   non-HOT: production has 156k HOT out of 929k updates, so each row also writes
   about ten index entries. `last_seen_at` is never read anywhere.
3. **Incremental filter sweep does a full table scan every 10 minutes.** The
   "posting date: unavailable" UPDATE was planned as a full table scan (717 MB,
   36–250 s per call). PostgreSQL could not estimate its OR-heavy predicate
   (estimated 58k rows; it actually changes about 0). Never-filtered duplicates
   also bloat the 0009 partial indexes.
4. **Change detection reads TOAST for every listed posting.** The JSONB
   comparison averaged 0.9 s per 25 rows (112 s per run), even when nothing
   changed.
5. **Per-claim schedule aggregate.** `due_providers` ran 202 times at 0.45 s each
   (91 s per run).
6. **HTML-to-text normalization of unchanged boards.** About 670 s of CPU time
   across threads per run. This becomes the limit once the disk is fixed.

## Changes (all tested: 971 tests pass)

| # | Change | Removes |
|---|---|---|
| 1 | Ingestion writes only reopened rows. An open job's last sighting is `board_state.last_success_at`. | bottleneck 2 |
| 2 | Board snapshot digest (`board_state.snapshot_hash`, migration 0011). It is computed from the raw source records *before* normalization, with volatile keys (Workday `postedOn`), the normalizer code and ingestion parameters included. An identical listing skips normalization and every `jobs` read/write. Digests are trusted for 24 h, so every board is still fully re-ingested daily. | 4, 6 for unchanged boards |
| 3 | Incremental application linking: only jobs inserted since the last pass, with a 2,000-id overlap. A link is never moved to an older listing. Full pass when applications change or daily, in its own transaction. | 1 |
| 4 | Filter sweep: `SET LOCAL enable_seqscan = off` for incremental passes, plus status-restricted partial indexes (migration 0012, built CONCURRENTLY). A policy-change recheck still scans normally. | 3 |
| 5 | Workday/Personio stored-detail lookup checks heap columns first (posted_at / description hash) before unpacking payloads. | per-poll TOAST reads |
| 6 | `due_providers` cached for 10 s (refreshed if a claim comes back empty while slots are free). | 5 |
| 7 | `workers.sh` waits while the legacy cycle is "activating". | deploy race seen on Oct 20 |

## Before / after

| Run | Code | Workload | Collectors | Boards/min (median) | Disk busy | CPU waiting on disk | Notes |
|---|---|---|---|---|---|---|---|
| A1 | rc2 | first pass | 1 | 9 | 82% | 56% | link 450 s of maintenance |
| A2 | rc2 | first pass | 2 | 14 | 77% | 46% | |
| A4 | rc2 | first pass | 4 | 14 | 78% | 30% | link query 718 s |
| B1 | fixes 1–2, 3 partial | first pass | 1 | 16.5 | 77% | 54% | sweep table scan 273 s |
| D4 | fixes 1–3, 5–6 | first pass | 4 | 17 | 83% | 40% | sweep table scan 499 s |
| F4m | **all** | digested boards first, then the rest | 4 | 80–159 while unchanged | 43% | 30% | 265/265 ok, 256 skipped |
| F4s | **all** | steady state (unchanged) | 4 | 214 (265 boards in 1.3 min) | 21% | 15% | CPU 76% idle |
| F1s | **all** | steady state (unchanged) | 1 | 136 | 16% | 11% | p50 0.26 s/board |

B2 is excluded: one of my diagnostic queries ran a full row count during it.
D4s/D2s are excluded: an unbound-variable bug in the skip path made unchanged
polls fail. It is fixed, and a regression test covers it.

Memory in every run: worker 120–270 MiB, PostgreSQL 112–162 MiB. No OOM kills;
swap-out averaged 0.14–0.3 MB/s.

## Expected sustainable throughput on 1 GB / pd-standard

- **Unchanged boards:** 120–140/min with 1–2 collectors, limited by HTTP rather
  than disk or CPU.
- **Changed boards:** limited by random reads, about 2–10 s each. In F4m, 9 of 265
  boards (3.4%) had changed after ~75 minutes.
- **Full rotation of 12,237 boards** at ~96% unchanged: **~1.5–2 hours**, compared
  with ~2 days for the legacy cycle and ~15 hours for the trial build. The
  30-minute target revisit interval is not reachable on this disk. Boards stay
  slightly overdue and are taken most-overdue first.
- **First pass after deployment** (no digests yet): every board is ingested
  normally at ~17/min, about **12 hours** of a busy disk, once.
- **Daily costs:** one full application-link pass (~9 minutes of saturated disk)
  and the backup, which stops the workers.

## Remaining limits

- The sweep's pending-job SELECT still costs 25–245 s per 10 minutes, as random
  reads through `ix_jobs_pending_active` with heap checks of `posted_at`. The
  next step is an index or predicate on `posted_at` for the 72-hour window.
- Changed boards still compare every stored row (TOAST). A per-row digest would
  remove that.
- No run lasted longer than 15 minutes. A 24-hour supervised trial is still
  needed.
- Resource option, not required: a pd-balanced 30 GB disk (~3,000 IOPS) would
  remove the random-read ceiling for about $2 more a month.
