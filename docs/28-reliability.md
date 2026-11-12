# Supervision, verified recovery and online maintenance — November 6, 2026

Status (November 9): **1,243 passed on Linux/PostgreSQL 16; not deployed**.
Cloud Shell ran the revised packaged source on Linux x86-64, Python 3.12.3 and
PostgreSQL 16.15 using a private Unix socket. The first run exposed a flaky
boundary test: it calculated expiry from a higher-precision clock reading than
the timestamp in the backup marker. The corrected test checks immediately before
and exactly at expiry using the stored instant. No production cleanup rule changed.
The full rerun passed in 194.57 seconds with exit 0; the test database stopped.
The log is `~/reliability-release-20261109/native-pg16-suite.log` in Cloud Shell.
The revised archive SHA-256 is
`a1dcd5367cc974f2837ae76124d2032a9cac8cc8a7249199d418779422a45992`.
`bash deploy/vm/test-source-pg16.sh` reproduces this check from that package;
it cannot issue the deployment pass file for a production image.

The exact production image could not be inspected or tested. The current Cloud
Shell configuration and the default gcloud credential store listed no account;
an explicit-account read of the VM returned “does not have any valid credentials.”
Browser SSH did not establish a terminal. The PostgreSQL container pull also
timed out. No application deployment, VM/disk change or AI budget change was made.

### Existing production health (November 9, Eastern time)

| Priority | Observation | Evidence and next action |
|---|---|---|
| P1 | Matching is failing while the worker is running. | VM serial logs show 588 `401 invalid x-api-key` entries between 1:49 and 9:01 a.m. Filters were still processing jobs at 9:01. Secret Manager shows one enabled Anthropic secret version; provider acceptance is separate. Supply a valid key via Secret Manager and update the configured version reference when rotating (`cloud_io.py` currently requests version 1). Confirm matching succeeds before claiming recovery. |
| P1 | The watchdog reports down. | `progress.json` last advanced at 4:19 a.m.; `incident.json` recorded no successful scan at 5:00 a.m. Later filter logs confirm partial worker activity, not a healthy end-to-end pipeline. The reason heartbeat publication stopped is unconfirmed. |
| P2 | Listener health is unknown. | VM running state and worker metrics do not verify the listener. Check its application status after authenticated host access is restored. |
| Verified | Existing backup completed. | The journal records a verified archive stream, checksum-confirmed upload and successful backup service exit at 3:03 a.m. `backup.json` confirms `vm-backups/day-0.dump`, 1,181,835,127 bytes, in 2,513.6 seconds. All seven dated objects were present. This validates the old backup job; no restore or new online-backup rollout is claimed. |

## Scope and costs

Retain the existing VM, disk, PostgreSQL, Cloud Storage bucket, seven daily backup
slots, Cloud Run watchdog schedule, job filters, model selection and AI budget.
No new paid resources, larger machines, extra AI budget or Cloud Build jobs.
The new recovery timer and status files run locally. Heartbeat frequency remains
five minutes; the watchdog no longer queries the retired Cloud Run polling job.

## Supervision and recovery

The worker checks required thread liveness and progress every five seconds.
An exited thread or a stage without a loop checkpoint for 20 minutes fails the
process. A collection has a separate 30-minute deadline. Empty queues, AI budget
pauses, backpressure and backup pauses continue checkpointing and are not stalls.
Worker SQL statements are limited to five minutes, lock waits to ten seconds,
and idle transactions to one minute. Listener statements have a one-minute bound.

Shutdown drains threads for at most 90 seconds. If work remains alive, leases are
not released early: the CLI terminates the process and lets its leases expire.
This avoids a second owner processing work while an old thread is still writing.
Thread-safe status generation does not hold locks across database requests.

`/run/jobradar/status/worker.json` is written atomically by the supervisor.
The listener publishes its own status after polling, including connection errors.
The separate heartbeat thread reports stage health even when maintenance is busy.

systemd permits three worker starts per 30 minutes. A local one-minute recovery
timer catches stale processes and services left stopped after failed maintenance.
It respects disabled workers, maintenance locks and startup grace periods; its
own repairs are capped at three per 30 minutes. It does not restart PostgreSQL.
Recovery verifies fresh application status instead of trusting a successful
`systemctl start`. A connection error in the listener is reported, not treated as
proof its process is dead. Failed verification remains an error in the journal.

## Online backups

Backups take the operations lock exclusively and share the maintenance lock with
workers. The legacy polling path still takes the maintenance lock exclusively.
An atomic, three-hour maintenance lease requests low-load mode. Collection,
filtering, matching, discovery and cleanup stop accepting new work; existing work
drains. The listener, notification delivery, lease keeper and heartbeat continue.
The backup waits up to ten minutes for a fresh acknowledgement bearing its exact
lease token. Without acknowledgement it aborts without shutting services down.

The dump still uses `pg_dump -Fc -Z1`, a 90-minute maximum, full archive validation,
and size/checksum verification after upload. `timeout` runs inside the PostgreSQL
container, so the dump is bounded even if the Docker client dies. Host cancellation
also attempts to terminate the specifically named backup database session.
Validation has a 30-minute in-container bound. The previous verified local archive
survives dump/validation failure; the previous remote object survives before a
successful replacement. Heavy work resumes before the network upload begins.
If the controller is killed without cleanup, the maintenance lease expires.

## Storage cleanup

Cleanup is enabled in the worker configuration but gated by a successful uploaded
backup less than 36 hours old. The root-owned approval file lives in `/run`; after
reboot cleanup waits for the next verified backup. This gate does not stop scans.

Every five minutes, at most one transaction considers a primary-key window of
2,000 rows and prunes at most 25 eligible rows. Each SQL statement has a three-second
limit and lock acquisition has a 100-millisecond limit. Timeouts roll back the slice
and yield until the next interval. Progress is stored transactionally in `app_state`
and wraps at the end of the table. A transaction advisory lock prevents concurrent
cleanup. Locked rows are skipped and revisited on a later pass.

Existing eligibility safeguards are preserved: only old filtered/duplicate content
is emptied; sponsorship-review holds and open detail-cached postings are retained.
IDs, application/feedback records, dates, URLs and deduplication hashes remain.
No automatic table rewrite, `VACUUM FULL`, schema migration, index removal or
unmeasured autovacuum tuning is included. Space becomes reusable; it does not
necessarily shrink the disk file. The initial pruning backlog may take weeks.

## Validation and release gates

Tests exercise dead and stalled stages, healthy idle loops, live notification
delivery during maintenance, lease expiry, stale recovery evidence, bounded repairs,
backup timeout/validation failures, cancellation, retained prior archives, checksum
checking, cleanup cursor persistence, locked rows, and rollback on SQL timeout.
The full local suite uses isolated PostgreSQL 17; no real Anthropic or Telegram
requests are made. Two lifecycle fixture builders now use today's stable UTC date
so calendar time cannot silently age test postings out of ingestion. Migration
scratch databases explicitly use UTF-8, and lock tests work on macOS and Linux.

Final local result: **1,243 passed, zero failures, zero skips**, in 64.14 seconds.
Python compilation, shell syntax checks and `git diff --check` passed. The raw
result is retained in `.local/reliability-20261106/all-tests-final.log`.
The later native Linux/PostgreSQL 16 result is recorded above. Neither source
run substitutes for the memory-limited test of the exact deployment image.

Prepare a package with:

```sh
.venv/bin/python deploy/vm/package-reliability.py .local/reliability-release-20261106
```

The explicit package excludes `.env`, Git history, local databases, private keys
and local logs. It includes source/test files and SHA-256 manifests. Build the
small code overlay on the verified current production image with
`reliability.Dockerfile`; it preserves the base image's profile and dependencies.

Before deployment, run `bash test-reliability.sh IMAGE` in the package directory on
a working Docker/Linux host. It runs the full suite against PostgreSQL 16 on an
internal Docker network with 512 MiB worker and 256 MiB database memory limits.
Only a passing run writes evidence tied to the image ID and package manifest.
Temporary test containers, volumes, network and test image are removed afterward.

`apply-reliability.sh NEW_IMAGE EXPECTED_CURRENT_IMAGE ANTHROPIC_VERSION` refuses to deploy without
that evidence. It also refuses an unexpected live image or changed host files,
active backup/compaction/prune, or active legacy polling. It preserves root-only
rollback copies, drains old activity, installs the services, and verifies fresh
worker/listener health before enabling recovery. Failed deployment restores the
previous image and host configuration; old-version recovery must then be checked
in the journal because it has no new status files.

After rollout, observe one complete online backup and confirm Telegram remains
responsive, collection resumes, `backup.json` advances and cleanup commits a slice.
Update the existing Cloud Run watchdog to the tested image without changing its
schedule, resource limits, secrets or AI settings. Reboot verification and a longer
disk/latency observation remain live rollout checks, not claims of local testing.

## Anthropic key cutover and exact-image verification

The rotation package is staged, not deployed. Its SHA-256 is
`4c328fc00faa313ddf0ca7c08558c98e68596e643d85c64a592a78b85984c1b0`.
On November 9, the archive checksum matched in Cloud Shell. The rotation
source suite passed all 1,245 tests in 174.19 seconds on Linux with PostgreSQL
16.15; the temporary server stopped and the runner exited 0. This supersedes
the earlier source run. It does not establish that the candidate image passed.

1. Verify CLI identity in the actual release terminal with `gcloud auth list
   --filter=status:ACTIVE --format='value(account)'`, then describe
   `jobradar-vm` in `jobradar-510801/us-east1-b`. A signed-in console page does
   not prove that the CLI can make this call.
2. Create a replacement in the existing Anthropic workspace, with the same
   permissions and spend limits. Enter the key directly in the existing
   `jobradar-anthropic-key` Secret Manager resource. Use only its numeric
   version in release commands. Verify billing-account headroom before adding
   a version: both enabled and disabled versions count for Secret Manager
   pricing. Do not destroy the rollback version to make room without review.
3. Read the VM's configured immutable image reference and compare its local
   image ID to the running worker and listener IDs. Investigate any mismatch.
   Build `reliability.Dockerfile` on that digest in the existing Cloud Shell.
   Run `bash test-reliability.sh CANDIDATE` in the extracted bundle. Pytest and
   fixtures are mounted read-only; the worker container runs the original
   candidate ID, with a separate PG16 container and no production credentials.
   The runner checks the container ID, database major version, exit and OOM
   state before recording the pass. Transfer that evidence with the release.
4. Run the guarded apply script with the tested immutable candidate, observed
   base and replacement version. Before stopping services, it fetches that
   version using the VM identity and makes a Models API GET (no inference).
   It stages only the changed line in a private copy of the existing env file.
   Cutover records the number in `/opt/jobradar/anthropic-secret-version` for
   subsequent credential refreshes; Telegram remains on its existing pins.
   Failed apply restores the prior runtime env and version pin.
5. Verify a real queued match succeeds under the existing budget, without
   submitting a synthetic inference. Check fresh listener polls without errors,
   two advancing external heartbeat timestamps, then one complete online dump
   and checksum-verified upload. Check that the listener and heartbeat continue
   during the dump and that collection resumes after maintenance. Record the
   image ID, match time, heartbeat times and backup generation/size. Retire the
   old provider key after checking its consumers. Disabling a Google secret
   version does not revoke the provider key or remove version storage charges.
