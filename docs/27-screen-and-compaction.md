# Two-stage AI scoring and a one-pass table compaction (Oct 29, 2026)

The user asked for application optimizations rather than a larger VM or a bigger
AI budget.

## Situation (production, Oct 29)

| Measure | Value |
|---|---|
| Nightly backup | failed again Oct 29 (07:23–08:53 UTC): the dump outran the new 90-min limit. Last good backup: Oct 28 14:33 UTC |
| jobs table | 6.4 GB: heap 990 MB, TOAST 4.7 GB, 12 indexes 705 MB; 1.06M rows |
| Disk | 30 GB pd-standard, 12 GB free; sequential read 16.8 MB/s (300 MB, direct I/O), little random I/O |
| Batch prune (Oct 28) | ~10 rows/s: each row writes a new tuple plus 12 index entries, all random I/O |
| AI scoring | Haiku 4.5, ~4,500 input + ~355 output tokens a call, 1.9 calls a job, $0.0117 a job: ~86 jobs a day for $1 |

## 1. Two-stage scoring: Haiku 5.5 screen, Haiku 4.5 score

Haiku 5.5 is priced at $0.10/$0.50 per million tokens against $1/$5 for Haiku 4.5.
Measured on 23 already-scored production jobs (read-only, nothing stored, ~$0.05):

| | Haiku 4.5 (stored) | Haiku 5.5 |
|---|---|---|
| Cost per job | $0.0117 | $0.0020 (6,162 in / 1,371 out tokens a call, 1.5 calls a job) |
| Mean score difference | | −8.4 (median absolute 10) |
| The 90 | 90 | 74 |
| 80–83.5 | 6 jobs | 0 (70–74, and 0 for a Vancouver role it correctly found outside the US) |
| Failures | | 1 of 24: ran out of its 2,048-token answer budget |

Haiku 5.5 alone would make 90+ alerts close to impossible without changing the
threshold the user chose. So both models are used (`matcher.assess`):

- Every job is first assessed by Haiku 5.5 (same prompt, same review step, 4,096
  answer tokens).
- Below 60, its assessment is stored as the job's score (it can't alert at 90).
- At 60 or more, Haiku 4.5 assesses the job as before; its score is stored and
  drives alerts, so 90 keeps its meaning. The gate sits well below the screen's
  equivalent of 90 (74) and of 80 (70–74).
- `job_matches.model` records which model produced the stored score.
- The screen isn't part of the cache key (`context_hash`), so deploying it
  re-scores nothing. `JOBRADAR_SCREEN_MODEL=off` turns it off;
  `JOBRADAR_SCREEN_MIN_SCORE` moves the gate.

Expected cost: ~$0.002 a job plus $0.0117 for the share that passes the gate (none
of the 12 most recent scored jobs did; 10 of the 11 best ever did), roughly
$0.003–0.004 a job: about 250–330 jobs a day for the same $1, against ~86. The
hourly pacing (docs/26) is unchanged.

## 2. One-pass prune and compaction

`jobradar prune-content --rewrite --yes` (`prune.rewrite_prune`) runs a single

    ALTER TABLE jobs
      ALTER COLUMN raw TYPE jsonb USING CASE WHEN <prunable> THEN '{}' ELSE raw END,
      ALTER COLUMN description TYPE text USING CASE WHEN <prunable> THEN '' ELSE description END,
      ALTER COLUMN raw_digest TYPE text USING CASE WHEN <prunable> THEN <digest> ELSE raw_digest END

A `USING` clause that isn't the column itself makes PostgreSQL rewrite the whole
table once, evaluating all three expressions on the old row, and rebuild every
index from the new table. Same rows, same digest and same safety rules as the
batch prune (`prunable()`, `_digest_by_source()`), but the work becomes
sequential: one read of the table, one write of the smaller copy, 12 index
builds. The file shrinks at once (the batch prune also needed `VACUUM FULL`).
Then `VACUUM (ANALYZE) jobs`.

It is one transaction under an exclusive lock: workers must be stopped. A
failure or the time limit (`--max-hours`, default 5) rolls everything back.
Session settings: `maintenance_work_mem` 64 MB (the database container has 256 MB),
no parallel workers.

`deploy/vm/compact-jobs.sh [minutes]` wraps it for the VM:
- refuses to run during a backup;
- stops the workers and the listener and waits for an idle database;
- checks the free disk (the table size plus 2 GB);
- runs the rewrite under the exclusive maintenance lock;
- restarts whatever was running, even after a failure.

Planned run: Oct 30 01:45 UTC (9:45 PM ET), at most 270 minutes, so it ends before
the 07:17 UTC backup. Expected: about 35% of 1.06M rows emptied, the table from
6.4 GB to roughly 3.8 GB, a faster dump.

The table grows again by roughly 1 GB a month (~11k new rows a day). The same
command can be rerun when the dump time approaches its limit.

## Tests

- 8 for two-stage scoring: gate below and above, gate setting, the screen's own
  review step, turning it off, cache key unchanged, stored model.
- 6 for the rewrite: same rows and digests as the batch prune (including Workday
  volatile keys), indexes rebuilt and valid, table rewritten, unchanged postings
  not rewritten afterwards, rollback on timeout, CLI.
- Test runs default to a single model (`tests/conftest.py`).
- All 1,220 pass.

Build: Cloud Build `228f627b-8f9c-49dc-b25a-7bb5566a8555`, an overlay on
`worker@sha256:b71c516f…` with a verified base source hash `2234c17f…` and patched
hash `f576cec7…`; smoke test passed. Image
`worker@sha256:87b965f1377a2546a70766eb4b1d9febe82a2509af78b6b2ba5cb0cc5666899f`
(tag `oct29-screen-rewrite`).

## Rollout (Oct 29)

| Time (UTC) | Step |
|---|---|
| 14:52–14:54 | Guarded deploy of `87b965f1…` (user-approved): workers and listener stopped, database idle at once, schema stays 0016, restart. Filter signature unchanged (`179fbdd2…`). `.env` backup `.env.bak-20261029T145218Z` |
| 14:55 | `/opt/jobradar/compact-jobs.sh` installed (hash `72e86d12…`); one-off timer `jobradar-compact.timer` for Oct 30 01:45 UTC, 270 minutes at most |
| 15:04–15:07 | `654f9349…` deployed: the relevant-board fast lane is off (docs/26, update). `.env` backup `.env.bak-20261029T150431Z` |

Today's $1 was spent before the deploy, so the screen first runs at 00:00 UTC
(8 PM ET).
