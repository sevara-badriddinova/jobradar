# Failed-run audit — October 19, 2026

Read-only audit of the active Google Cloud VM, PostgreSQL run history, systemd
journals, kernel and PostgreSQL logs, backup objects, and pipeline source.
No production settings, data, schedules, or application code were changed.
Times below are America/New_York (EDT), unless explicitly marked UTC.

## Scope and result

The fixed run-count window is **October 16, 10:04:18 AM through October 19,
8:30 AM EDT** (after VM cutover). There were **14,950 board attempts**, of which
**381 failed (2.55%)**, with no unfinished records in that fixed window.
These are individual board attempts, not whole pipeline executions.

The VM journal records **two failed whole-cycle services** after cutover:
one Telegram timeout and one 45-minute service timeout. Separately, PostgreSQL
was killed for exceeding its container memory limit twice, and all three
scheduled daily backups failed. The application continues to run: PostgreSQL
and the Telegram listener were up, and the inspected heartbeat recorded a
completed pipeline at **8:33:27 AM on October 19**. That proves progress at that
time, not that every board succeeded or that backups are healthy.

The latest three executions of the **retired Cloud Run poll job** were also
failed: `jobradar-poll-qf5rt`, `jobradar-poll-42hft`, and
`jobradar-poll-fv487`, launched October 16 at 9:05, 9:00, and 8:55 AM.
Cloud Logging for `qf5rt` confirms PostgreSQL connection rejection because the
Neon account/project exceeded quota, before acquiring the cycle lock. These
pre-cutover failures are a separate historical incident, not evidence that the
current VM still connects to Neon.

## 1. Daily backups have failed three days in a row — high priority

| Date | Backup start | Termination | Result |
|---|---|---|---|
| October 17 | 3:19:07 AM | 3:29:07 AM | 600-second timeout, SIGTERM |
| October 18 | 3:20:41 AM | 3:30:42 AM | 600-second timeout, SIGTERM |
| October 19 | 3:18:32 AM | 3:28:32 AM | 600-second timeout, SIGTERM |

The backup bucket contained only `vm-backups/day-4.dump`, uploaded October 16
at 10:02:45 AM, 111,849,656 bytes. No October 17–19 backup objects existed at
inspection. The local staging dump was 230,125,568 bytes, last modified at
3:28:33 AM on October 19, at the timeout boundary. It must not be treated as a
verified recoverable backup.

**Confirmed cause:** systemd terminates the backup after ten minutes
(`deploy/vm/install-services.sh:59`). The implementation writes a complete
`pg_dump -Fc` before uploading (`deploy/vm/cloud_io.py:68`). The staging file's
write time and absent new cloud object strongly indicate the dump was still
being produced when killed. Logs do not include phase timings, so the exact
dump bottleneck cannot be measured retrospectively.

The database is now approximately 4,853 MB, while the VM has 1 GB RAM and a
30 GB standard persistent disk. The production workload shows substantial disk
reads. Dumping the growing database concurrently with that workload is a likely
contributor. Disk capacity itself was not exhausted: 18 GB remained free.

**Repair direction:** measure dump and upload separately; allow an appropriate
bounded backup duration; coordinate backup I/O with polling; publish only a
completed archive; verify restoration in an isolated database. Add backup-age
monitoring. Merely extending the timeout does not prove a backup is valid.
Also replace the whole-file `path.read_bytes()` upload with streaming before
larger archives introduce another memory pressure source.

## 2. PostgreSQL exceeded its memory limit twice — high priority

Both failures occurred while ingesting the Recruitee `devfinders` board:

| Run ID | Attempt started | Kernel kill | Recovery ready |
|---|---|---|---|
| 9365 | October 16, 1:45:30 PM | 1:50:20 PM | 1:50:45 PM |
| 21627 | October 19, 12:47:57 AM | 12:50:29 AM | 12:52:24 AM |

The kernel explicitly logged `Memory cgroup out of memory: Killed process ...
(postgres)`. PostgreSQL logged its backend being killed by signal 9, terminated
its other server processes, and reinitialized. This was a container memory-limit
failure, not a Neon quota error or an ordinary upstream HTTP failure.

The PostgreSQL container is limited to **256 MiB RAM and 512 MiB RAM-plus-swap**,
with 64 MB shared buffers (`deploy/vm/compose.yaml`). Run errors identify the
ingestion query comparing stored JSONB with incoming payloads. That code passes
up to **250 complete raw job payloads per VALUES batch**
(`src/jobradar/repository.py:79`), bounded by row count rather than bytes.

**Confirmed:** OOM during that ingestion query, recurring on the same board.
**Not yet isolated:** the exact query-plan node or payload-size threshold that
causes the memory peak. Replaying the crash against production was deliberately
avoided.

**Repair direction:** reproduce with the same board in an isolated constrained
database; bound incoming JSON by bytes as well as row count and inspect the
comparison plan. Validate a total VM/container memory budget. Avoid blindly
raising every memory limit on the 1 GB host.

## 3. Database work is slowing coverage and caused a full-cycle timeout

Completed-stage medians from systemd journal timestamps, in seconds. Day groups
here use UTC, matching the source logs; October 16 and 19 are partial days.

| UTC date | Collection | Filtering | Matching | Notification stage |
|---|---:|---:|---:|---:|
| October 16 | 173.8 | 86.0 | 36.0 | 98.7 |
| October 17 | 125.3 | 171.6 | 71.8 | 289.0 |
| October 18 | 147.3 | 339.4 | 137.9 | 470.3 |
| October 19 | 134.5 | 459.0 | 108.4 | 570.7 |

These are medians of individual completed stages, not measured median total
cycle durations. The notification stage includes application linking, filtering,
and queue preparation; its duration is not evidence of slow Telegram delivery.
For example, a completed notification stage took 10 minutes 31 seconds while
reporting zero queued or sent alerts and zero delivery failures.

**Specific failed cycle:** October 18 began collection at 7:15:01 AM; collection
completed successfully at 7:53:25 AM after 2,304 seconds. The batch included a
slow Accenture Workday scan and a logged detail-request timeout. Filtering then
ran until systemd terminated the service at 7:59:41 AM, reaching its configured
2,700-second total limit. Matching and notification were not reached in that
cycle. Collection exhausted most of the budget; slow filtering exhausted the rest.

**Database evidence:**

- The jobs relation occupied 797 MB of heap and 4,836 MB including indexes and
  TOAST. Its statistics estimated 798,239 live tuples and 127,972 dead tuples.
  These are estimates, not exact job counts or a measured bloat percentage.
- At inspection, jobs statistics showed 267 sequential scans and 152,426,404
  tuples read by those scans. Statistics were affected by the database crashes;
  do not treat them as full cutover-window totals.
- Docker reported approximately 2.02 TB of cumulative PostgreSQL block reads
  since container start. This is disk I/O, **not network egress or billable Neon
  transfer**.
- A live pending-filter SELECT was waiting on `IO / DataFileRead` after 29 seconds.
- A nonexecuting EXPLAIN of the equivalent pending-filter query chose
  `Index Scan using jobs_pkey`, with only `id > 0` as the index condition; date,
  status, and filtered-state predicates were applied as filters. A 250-row LIMIT
  therefore does not bound how many older rows must be examined. No EXPLAIN
  ANALYZE of this expensive production query was run.

The five-minute timer prevents overlap; it does not guarantee a completed scan
every five minutes. Only **3,250 distinct boards** were attempted in the preceding
24 hours. At that rate, 12,237 boards imply approximately **90 hours** for a full
rotation if throughput remains similar, longer than the current 72-hour posting
window. This is a projection, not an observed rotation duration. At inspection,
all enabled boards had at least one attempt in the previous 72 hours, but **335
had no successful attempt** in that interval.

**Repair direction:** optimize pending/expiry filtering and application linking
for the larger dataset, verify supporting indexes and planner statistics, and
avoid redundant full-dataset work in both filter and notify. Measure per-board
and per-stage budgets. Retest on the actual small-VM resource envelope before
changing batch size or cadence.

## 4. One transient Telegram read timeout stopped a pipeline

On October 16 at **8:20:36 PM**, `getUpdates` raised `ReadTimeout`; notification
returned exit 2. systemd labeled that numeric status `INVALIDARGUMENT`, but the
application log identifies a Telegram read timeout, **not invalid CLI syntax**.
The next cycle started immediately afterward.

`Telegram.get_updates()` deliberately makes one attempt and uses `timeout + 5`
seconds as its HTTP timeout (`src/jobradar/telegram.py`). `cmd_notify` returns 2
when update processing raises `TelegramError` (`src/jobradar/cli.py:313`).

**Repair direction:** consider bounded retries for safe update reads and review
whether a transient update-poll failure should prevent notification preparation.
Retain conservative retry behavior for message sends with uncertain delivery.
There is no evidence here of a bad Telegram token or a lost job-alert send.

## 5. Most board failures are endpoint or response failures

| Failure | Attempts | Interpretation |
|---|---:|---|
| HTTP 404 | 244 | Requested board endpoints were not found |
| Invalid JSON | 124 | All from Workday; response could not be decoded |
| HTTP 403 | 5 | Endpoint refused access |
| No posting details fetched | 3 | Every attempted detail retrieval failed |
| PostgreSQL OperationalError | 2 | The confirmed OOM incidents above |
| HTTP 422 | 2 | Endpoint rejected the request |
| HTTP 500 after four attempts | 1 | Upstream server failure exhausted retries |
| **Total** | **381** | |

Failed attempts by source: Workday 131, Greenhouse 116, Ashby 56, Rippling 26,
Personio 24, Recruitee 13, Workable 13, Lever 2.
Collectors can also return usable partial snapshots with `ok=true`; the failure
counts above therefore do not measure every skipped posting or incomplete board.

Recent 404 examples were Ashby `finvest`, `empora`, and `egra`, plus Lever EU
`expana`. A 404 alone does not establish whether the employer closed its board,
renamed it, migrated ATS, or the public directory has an incorrect address.
The registry validates URL structure but does not validate that a board is live.
There is no failure cooldown or persistent quarantine in the selection policy;
failed boards return on the ordinary rotation.

The 124 Workday JSON failures occurred between October 16, 11:15:30 AM and
October 17, 8:15:50 AM EDT. Read-only first-page probes from the VM during this
audit returned HTTP 200 and valid JSON for both `qnity.wd503` (434 postings)
and `pipersandler.wd501` (53 postings). This confirms those endpoints currently
work, not that every affected board or every pagination/detail request recovered.
Historical response bodies and content types were not retained, so an HTML
error page, maintenance response, rate protection, or malformed payload cannot
be distinguished retrospectively. The HTTP helper raises immediately on invalid
JSON even for HTTP 200; it does not retry that condition.

**Repair direction:** classify and retain safe response metadata, use bounded
retry for transient malformed responses, validate replacement URLs before
changing registry entries, and add cooldowns for repeated permanent failures.
Do not disable all Workday boards based on these historical errors.

## Monitoring gap and next steps

The independent watchdog checks pipeline progress and the retired Cloud Run
poll execution history. It does not inspect backup age, PostgreSQL OOM events,
per-board success coverage, or stage latency (`src/jobradar/health.py:26`). A
healthy heartbeat can therefore coexist with all of the issues above.
The saved incident object's `checked_at` changes only on health transitions,
so it is not itself a reliable last-monitor-run timestamp.
The three latest watchdog executions returned by the API during this audit
completed successfully: `jobradar-health-97t2d` at 8:30:23 AM,
`jobradar-health-npgzn` at 8:15:23 AM, and `jobradar-health-59nls` at 8:01:22 AM
on October 19. Successful monitor execution does not validate backup coverage.

Recommended order: restore and verify daily backup coverage; eliminate the
reproducible ingestion OOM; reduce database scan cost; add monitoring for backup
age and coverage; then address transient API handling and stale registry entries.
This audit establishes the failures and likely contributing mechanisms; fixes
and production validation remain separate work.

## Repair verification — October 19

### Confirmed crash reproduction

The live public `devfinders` Recruitee response contained 94 postings and
78,364,968 bytes of JSON (largest record 1,037,983 bytes). The previous 250-row
comparison sent the entire board in one statement. In an isolated PostgreSQL 16
container with the production 256 MiB memory / 512 MiB memory-plus-swap limits,
that exact statement failed after 2.37 seconds and incremented cgroup `oom_kill`
from 0 to 1. The repaired comparison processed the same payload in 93 bounded
batches in 2.28 seconds, with no additional OOM events. JSONB equality stays in
PostgreSQL, so key ordering and numeric representation do not cause false changes.
The limit is 25 rows and 512 KiB per statement, with oversized individual records
processed alone rather than discarded.

### Local regression evidence

All 879 unit and integration tests passed against an isolated PostgreSQL database.
New coverage includes the 25 MB large-board comparison, JSONB equality, batch
limits, policy/date corrections, migration consistency, and actual query plans
on 20,000 historical rows. Incremental filtering uses `ix_jobs_pending_filter`
and all three expiry updates use `ix_jobs_expiry_candidates` in that test.

### Repair design

- Backups use compression level 1, coordinate with polling through a host file
  lock, and have separate bounded dump, archive-read, and upload phases under a
  9,000-second service timeout. The previous successful local archive is retained
  until the replacement passes a full `pg_restore --file=/dev/null` read.
- Uploads stream from disk, retry transient errors from a fresh file handle, and
  verify Cloud Storage's returned size and MD5. `backup.json` records the object
  generation, SHA-256, size, archive verification, completion time, and duration.
- The watchdog can check that a verified backup is no more than 36 hours old via
  `JOBRADAR_CHECK_BACKUPS=1`, independently of the database and scan heartbeat.
- Migration 0009 builds indexes concurrently for company lookups, publication
  windows, pending filters, and rows that still need date-expiration checks.
  Failed concurrent builds can be retried; no job/application data is removed.
- Publication-window SQL exposes ordinary indexable range conditions while
  preserving missing-date behavior under both positive and negated predicates.
  Already expired/missing-date rows are skipped until content/date/policy changes.

Live deployment and timing evidence is recorded under "Production deployment" below.

The actual 94-row DevFinders payload also passed full ingestion and a repeat
idempotency check (`changed=0`) in the constrained test database. A new custom
archive created with the production `-Fc -Z1` options passed a full archive read
and restored into a separate empty database; counts matched across all 13 public
tables and schema version 0009. The round trip completed in 13.19 seconds without
another OOM. This is a local procedure test, not a restore of the new VM snapshot.

Safe-read follow-up changes retry a temporary HTTP-200 non-JSON ATS response once,
and give one-shot Telegram update reads one retry after a timeout/server error.
Interactive long polling still fails promptly to its existing retry loop;
uncertain message sends are never retried automatically. The expanded full suite
passed 883 tests.

## Production deployment — October 19

### Deployment

- The first attempt (image `…e34824b5`) failed because copied source files were
  unreadable by UID 10001 and rolled back cleanly; the database stayed at 0008.
- `deploy/vm/apply-repairs.sh` now pins the corrected image
  `worker@sha256:5e4668b4c9649a7d69b889f388bd643090a03786f13b0b6d8220d6301c292148`.
  It reuses the retained `/opt/jobradar/repairs-20261019/worker.env` only when that
  copy pins the previous image (`…50cc1e2e`) and is byte-identical to the live
  `.env`; it never overwrites it and also keeps a timestamped copy. It refuses to
  start with invalid indexes, and fails (restoring the old image setting) unless
  all indexes are valid and the schema is 0009 after the migration.
- Ran as transient unit `jobradar-apply-repairs-v2` at 14:08 UTC after the
  13:34 UTC backup (verified 1,152,592,917-byte upload, `backup.json` completed
  13:38:51 UTC). Migration 0008 → 0009 and a database-wide `ANALYZE` completed;
  result `success`. Listener healthy on the new image, poll timer active.
- Watchdog `jobradar-health` updated to the same digest with
  `JOBRADAR_CHECK_BACKUPS=1`; its verification execution exited 0 and reported
  `healthy`. The previous watchdog image is recorded in Cloud Shell at
  `~/health-prev-image.txt`.

### PostgreSQL OOM verification

PostgreSQL cgroup counters before: `oom 3`, `oom_kill 2` (limit 256 MiB).
Two DevFinders collections ran under the maintenance lock on the new image:

| Run | Result | Fetched | New | Seen | Changed | Collector secs | Wall secs |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | ok | 94 | 94 | 0 | 0 | 188.6 | 272 |
| 2 | ok | 94 | 0 | 94 | 0 | 76.6 | 137 |

Counters after both runs were unchanged (`oom 3`, `oom_kill 2`). The first run
ingested the 94 postings the earlier OOMs had blocked; the second confirmed
idempotency in production.

### Scan timing (first scheduled cycle on the new image)

| Stage | Before (12:33–14:08 cycles) | After (14:15 cycle) |
|---|---|---|
| filter | 7m15s–8m08s | 2m04s |
| match | ~1m35s | 1m32s |
| notify (incl. application linking) | ~9m14s | 3m05s |
| whole cycle | ~19m40s | ~12m |

Collect varied 1–5 minutes in both periods and is dominated by network/board
behaviour; the 14:16 collect exited 1 only because of an Ashby `gamut` HTTP 404.

### Remaining database hotspot (fixed in source, not yet deployed)

During notify, `link_applications` built its distinct job-employer set with a
parallel sequential scan of `jobs`: 96,618 pages read, 28.6 s per call. The new
`ix_jobs_company_key` was not used because only 61% of heap pages are all-visible.
The working-tree change enumerates employers with a recursive loose index scan
(one index probe per employer). Read-only comparison on production returned the
identical 228 relevant employer keys (same MD5) in 2.1 s versus 24.7 s; the key
enumeration alone took 113 ms with only buffer hits. The application/sync/
repair integration tests (41) and the full suite (879, excluding the
deploy-file-dependent `test_vm_backup.py`) passed against a disposable local
PostgreSQL 16. This needs an image build and deployment (no migration).

### Table growth and maintenance

`jobs` holds 800,569 rows (4.9 GB including TOAST and indexes); 650k were first
seen on October 16–17, mostly from very large Workday retail boards (Dollar Tree
20,145; CVS 19,780; Lowe's 12,703; TJX 11,163). 732,145 rows are `FILTERED` and
66,476 `DUPLICATE`. `jobs` has never been vacuumed (158k dead tuples) and
PostgreSQL runs with `shared_buffers=64MB`. A manual `VACUUM (ANALYZE) jobs`
under the maintenance lock would restore index-only scans; reviewing whether
large non-target employers belong in the registry would slow future growth.

### Remaining board failures (last 3 days, all `CollectorError`)

Workday 131 (mostly HTTP-200 invalid JSON on October 16–17, plus 403 from
`bozemanhealth` and `liveramp`), Greenhouse 117, Ashby 57, Rippling 27,
Personio 24, Workable 13, Recruitee 12, Lever 2 (`expana` 404). Recruitee also
logged the 2 OOM-era `OperationalError`s. New 404s since the audit include Ashby
`gamut`, Recruitee `forcit`, and Personio `ecosia`/`dr-schauer-partgmbb`.
Registry cleanup still needs per-board URL validation before edits.

