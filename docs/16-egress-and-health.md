# Network transfer repair and Telegram health monitoring — October 14, 2026

Status: deployed on October 14, 2026 through authorized Cloud Shell. The outage
alert was observed in the JobRadar Telegram chat at 09:27 EDT. The existing Neon quota block remains. These fixes do not reset
already-consumed transfer or change billing.

## Live deployment verification

- Cloud Build `935ab38c-ba4b-4812-b84c-db090ad1b09b` built
  `us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker:egress-20261014`.
- Both `jobradar-poll` and `jobradar-health` use the release. The watchdog uses
  512 MiB, the minimum accepted by this Cloud Run execution environment.
- Health execution `jobradar-health-mkf5z` completed successfully and logged
  `health transition delivered: down` at 13:27:15 UTC. The actual message was
  visually confirmed in the user's Telegram app.
- The five-minute `jobradar-health` scheduler is ENABLED. A repeat check invoked
  through the scheduler logged `health unchanged: down` at 13:27:55 UTC and
  exited successfully, confirming both invocation permissions and deduplication.
- The private bucket's `incident.json` records the current outage because the
  last three completed scans failed. Recovery remains untested in production
  until genuine pipeline progress resumes; no healthy heartbeat was fabricated.
- The application release passed 867 local tests before deployment. The sole
  deployment correction was the watchdog memory setting; shell syntax and
  whitespace checks passed afterward.

## Evidence and limits

The production Neon project reported 6,073,477,837 transferred bytes and its
console explicitly reported an exhausted monthly network allowance. Cloud Run
execution `jobradar-poll-4lbqt` failed at 08:50 EDT on October 14 while opening
the database connection, before any pipeline stage. Production SQL statistics
could not be read because that same connection is blocked.

Read-only aggregates on the local October 13 snapshot found 68,158 jobs and
12,237 enabled companies. The registry result serialized to about 2,491,164 bytes;
the first 25 registry rows serialized to 4,848 bytes. These are JSON payload-size
proxies, not captured PostgreSQL wire traffic. At 288 scans/day the old registry
selection alone projects to 717 MB/day. The new bounded selection reduces this
query's returned rows by 99.8%. This does not establish its exact contribution
to historical production usage.

The local snapshot contained 475,326,793 bytes of raw JSON versus 21,832,293 bytes
of the other ingestion-comparison fields. Downloading the raw JSON to compare
it with the just-fetched public API response was unnecessary. Returning a boolean
comparison instead removes roughly 95.6% of the compared payload on this snapshot.
It still uploads the incoming JSON to Postgres for comparison; that is ingress,
not outbound Neon network transfer. It does not remove stored evidence.

## Changes

- Company scan limits now execute in SQL; a 25-board scan no longer downloads
  the entire registry. Explicit name matching retains its previous behavior.
- Directory sync has a durable daily timestamp in `app_state`. Container-local
  cache files did not survive new Cloud Run executions, causing repeated
  directory refresh and full registry synchronization.
- Ingestion compares raw JSONB on the server in batches of 250 and returns a
  boolean. Content, locations, publication-date corrections, and reopening
  still invalidate the appropriate decisions. Notified jobs keep delivery
  history but have changed requirements filtered again.
- Filtering expires old, missing-date, and future-dated jobs in SQL. Unchanged
  decisions no longer download descriptions on every filter/notify invocation.
  Policy changes, explicit rechecks, content/date changes, and future dates
  becoming current still trigger evaluation. Titles are checked before bodies.
- Filter, matcher, and Telegram views select the needed location/date fields
  from raw JSON; full source HTML is not returned. All eight collectors retain
  their location interpretation.
- Matching applies its budget in SQL after invalidating stale scores, so the
  newest cached jobs do not starve older work. Invalidation still reads needed
  description/location inputs of unsent matched jobs to preserve correctness.
- Application linking selects only jobs with relevant company keys instead of
  returning all employers' jobs every time notifications run.

No schema migration, historical deletion, or eligibility-policy relaxation is
required. Existing application suppression and at-most-once job delivery remain.

## Independent health check

`python -m jobradar.health` runs as a separate Cloud Run job every five minutes.
It uses the existing Telegram credentials, reads at most 20 Cloud Run execution
records, and keeps two tiny JSON files in a private Google Cloud Storage bucket.
It never connects to Neon or consumes Telegram updates.

The polling worker writes `progress.json` only after all requested pipeline
stages finish without matching errors. Overlap skips cannot fake progress.
Isolated board failures may still permit the pipeline to finish; failed
prerequisites, matching errors, and failed Telegram delivery do not write progress.

An outage is detected after three consecutive failed executions without newer
pipeline progress, or after 30 minutes without pipeline progress. Detection can
lag by up to one five-minute monitor interval plus Cloud Run startup time. This
covers startup/database failures, a stuck cycle, and a stopped polling scheduler.
The first check during the current quota outage will send a real outage message.

`incident.json` uses generation-conditional writes to claim each transition.
An unchanged outage stays quiet. Recovery sends a second Telegram message.
Definite Telegram delivery failure is retried on the next check; uncertain
delivery is not automatically resent, preventing duplicates after a lost HTTP
response. The monitor cannot guarantee delivery during a Telegram outage, a
Google Cloud-wide outage, or loss of its own scheduler/storage permissions.

## Deployment

Review `bash deploy/egress-and-health.sh`; run with `--apply` after approval.
It builds from the existing allowlisted source, updates only the poller's image
and heartbeat environment, and preserves existing arguments, schedule, secrets,
model selection, and resource settings. It provisions the separate health job,
private state bucket, and five-minute trigger. The existing runtime identity
receives object access only on this bucket and a custom execution-list permission
only on the monitored polling job. The existing scheduler identity can invoke
the health job. No new service-account key or database credential is created.

The deployment consumes Cloud Build/Run/Scheduler/Storage resources and may
incur their normal charges. It is not an offline-only code change.

The script executes the health job and waits for completion. Verify its logs,
the incident object, and receipt of the Telegram outage message. Run it again
to verify suppression. Recovery must be verified after database access returns;
do not manufacture a successful production heartbeat while the pipeline is down.

## Further reductions and restoration

On October 14 the user requested faster coverage with bounded Neon usage.
The live collection batch was increased from 25 to 50 boards, preserving the
five-minute schedule and 25-job AI scoring budget. At 12,237 boards this needs
245 cycles, or about 20 hours 25 minutes with no skipped/failed cycles, instead
of 490 cycles / 40 hours 50 minutes. Actual coverage will be slower when runs
overlap or sources fail. This is a throughput ceiling, not a measured guarantee.
Collection is sequential and some boards are slow; a larger jump was avoided.
The local snapshot's 1,203 completed recent runs had a 0.33-second median but
14.48-second mean per board, with substantial outliers. Local timings cannot
establish cloud runtime after the query changes.

The SQL result remains limited to 50 company rows, comparisons still return
booleans instead of raw JSON, and unchanged jobs retain cached filter decisions.
More boards can increase total transfer even with those optimizations. There is
no hard byte-budget enforcement; the actual new transfer rate remains unmeasured
while production is quota-blocked. Revert only the collection argument to 25 if
post-restoration measurements show unacceptable usage or long cycles.

Rotation was additionally verified against local PostgreSQL with three consecutive
50-board cycles over 150 registered boards. Both all-success and all-failure
fetch scenarios selected disjoint batches across disposed/reopened connection
pools, then returned to the oldest batch only after all 150 were attempted.
The targeted registry, pipeline, and overlap-lock suite passed all 27 tests.
Attempt history is committed before fetching; a crash before a board's attempt
is recorded can legitimately leave that unprocessed board in the next batch.
No additional cache or production code change was required for rotation.

Deploying these fixes reduces future transfer; it cannot refund this month's
used quota. Database access still needs the allowance to reset or an authorized
plan change. Once restored, use read-only `pg_stat_statements` row/call totals
and Neon transfer deltas to measure the real post-fix rate. Do not reset shared
statistics or delete historical records just to measure savings.

Less frequent, larger polling batches can reduce connection and fixed query
overhead while preserving boards/hour. For example, 75 boards every 15 minutes
instead of 25 every five minutes preserves nominal coverage but adds latency
and needs cycle-duration validation. Merely slowing the schedule without
increasing batch size would worsen coverage under the 72-hour age policy.

Scale-to-zero only saves compute during idle gaps; it does not reduce transferred
bytes or fix the current quota block. The session lock intentionally keeps a
connection open during a scan. No compute or polling-frequency settings have
been changed as part of this repair.

Eliminating Neon altogether is possible because the application uses standard
PostgreSQL and local Docker Postgres already exists. Cloud polling would then
need a reliably reachable replacement database and an explicit data migration.
Moving everything back to the Mac would require it to stay awake and online.
The query fixes are useful with any PostgreSQL host and avoid that migration.

References: [Neon network transfer](https://neon.com/docs/introduction/network-transfer),
[Cloud Run execution listing](https://docs.cloud.google.com/run/docs/reference/rest/v2/projects.locations.jobs.executions/list),
[Cloud Storage conditional writes](https://docs.cloud.google.com/storage/docs/request-preconditions).
