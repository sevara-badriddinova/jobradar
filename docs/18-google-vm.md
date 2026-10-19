# Google Cloud VM migration

Status: deployed October 16, 2026; database restoration, full pipeline, private
backup, independent watchdog, and automatic reboot recovery verified.

## Reviewed target

- Existing project `jobradar-510801`, one `e2-micro` in `us-east1-b`.
- Debian 12, one 30 GB `pd-standard` boot/data disk, 2 GB swap within that disk.
- Expected baseline: about $3.65/month for IPv4 at 730 hours, provided the billing
  account's eligible VM and disk free allowances are unused. AI, taxes, existing
  cloud resources, backup storage, and outbound transfer over allowances are extra.
- Separate VPC and subnet. Only port 22 from Google's IAP range is allowed;
  PostgreSQL has no published host or internet port.
- Existing `jobradar-runtime` service account uses its existing three integration
  secrets and health bucket. Add only repository-scoped Artifact Registry Reader
  on `jobradar`, so the VM can pull the already deployed worker image. No keys.
- Preserve PostgreSQL 16, the full local
  database, all primary IDs, profiles, applications, reviews, outbox, and offsets.
  Neon remains inaccessible; cloud-only changes after October 13 cannot currently
  be reconciled. The active local database is the migration source.

## Architecture

On the VM, Docker runs PostgreSQL and the Telegram listener continuously. A
systemd timer invokes the same bounded `cycle --discover --collect-limit 50
--match --match-limit 25 --notify --digest` every five minutes. systemd and the
existing database advisory lock prevent overlapping scans. The existing Telegram
consumer lock serializes the listener and notification update processing.

The pipeline writes its Cloud Storage heartbeat only when matching and
notification stages complete. The independent Cloud Run watchdog is enabled
on a 15-minute schedule to limit Cloud Run minimum-duration
billing. Although it still reads the paused old poll job's
execution history, a newer heartbeat supersedes those historical failures. It
detects a stalled VM through 30-minute heartbeat staleness (normally within about
45 minutes plus watchdog startup). The old cloud poll
schedule remains paused, and local workers are stopped at cutover.

Daily pg_dump backups rotate through seven private objects under `vm-backups/`
in the existing health bucket. These preserve database history independently of
the VM disk. Provider disk encryption is retained; no public backup URLs.

The VM has deletion protection, and its disk is retained if the instance is later
deleted. Remove retained resources deliberately when decommissioning to avoid
continuing disk charges. It is a single host, with restart recovery and backups;
there is no automatic failover to a second VM.

Pricing references: [Compute Engine free allowances](https://docs.cloud.google.com/free/docs/free-cloud-features),
[IPv4 pricing](https://cloud.google.com/vpc/network-pricing), and
[Cloud Run watchdog pricing](https://cloud.google.com/run/pricing).

## Deployment files and cutover gates

`deploy/vm/provision.sh` creates the host and scoped network/access configuration.
`bootstrap.sh` installs Docker and swap; it cannot start the application.
`compose.yaml` defines memory-bounded services. `cloud_io.py` reads runtime secrets
via the metadata identity and writes root-only environment files. The installer
registers systemd services without enabling them.

Before cutover, stop local workers, take a fresh consistent dump and table-count
manifest, transfer it privately, restore into the empty VM database, and compare
counts and schema version. Validate memory, a complete pipeline cycle, actual
Telegram delivery, a backup, and restart persistence. Only then enable timers,
resume independent health monitoring, and mark deployment complete.

## Migration evidence

The final local writers stopped on October 16 before the snapshot. The backup
restored successfully into an empty VM database; all 13 public table counts and
schema `0008` matched. The source snapshot contained 84,570 jobs, 12,237 companies,
1,375 applications, 309 matches, two candidate profiles, 35 notifications,
47 feedback records, 20 reviews, eight review responses, and 828 processed emails.
The five application-state entries and 7,528 collector runs were also preserved.

The local migration backup is `.local/vm-migration-20261016/data/database.dump`.
Its SHA-256 is `083029ff5bba392303b10390adbf94bef173ab5ad222c347b2d209bb12004e6f`.
The archive checksum was checked at both Cloud Shell and the VM, and each payload
file was checked again before restoration. Both launchd workers are disabled and
unloaded. Private staging copies remain owner-readable only for recovery.

The pinned VM worker image is
`us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker@sha256:50cc1e2e81318efb414400aab3366230e1cf3ca0721e3a321fe2f534ca807e60`.
Its `profile.toml` checksum matches the local source exactly.

## Operations

Connect from Google Cloud Shell:

```bash
gcloud compute ssh jobradar-vm --project=jobradar-510801 --zone=us-east1-b --tunnel-through-iap
sudo systemctl status jobradar-stack jobradar-poll.timer jobradar-backup.timer
sudo journalctl -u jobradar-poll -n 60 --no-pager
sudo systemctl start jobradar-poll.service
sudo systemctl start jobradar-backup.service
```

Runtime settings live under root-only `/opt/jobradar`; the database directory and
password live under `/var/lib/jobradar`. Restarting `jobradar-stack` refreshes the
existing integration secrets through the VM identity. Do not run local workers
against the retained local database while the VM owns Telegram updates.

For disaster recovery, restore a private dump into a fresh empty PostgreSQL 16
database using `restore.sh` and the matching count manifest, or independently
verify the recovered database when restoring a daily dump. Preserve notification
and Telegram state; do not start a second consumer with a different database.

## Small-VM query optimization

The first cloud verification exposed an expensive application-history query: it
repeated company-prefix matching for each job/application pair. The VM image adds
one code change to the previous immutable image: compare distinct employer keys
in a materialized CTE, then fetch only jobs for those employers. Company and role
matching rules remain unchanged. All 66 application and egress regression tests
passed against an isolated local test database.

Cloud Build `225dd143-610f-48f5-a90c-3eed4b8f5f86` succeeded using the existing builder
identity. `deploy/vm/worker.Dockerfile` records the small image overlay. The normal
root Dockerfile also incorporates the same source change on future full builds.

The optimized live verification ran 13:58:38–14:00:05 UTC on October 16. Collection,
filtering, matching, notifications, and heartbeat publication all succeeded
(`CYCLE_EXIT=0`). Matching took 3.2 seconds; notification processing took 3.3 seconds.
No jobs in that batch passed the current profile, so no job alert was due. A live
Haiku request and Telegram authentication both succeeded from the VM.

The first private backup uploaded successfully as `vm-backups/day-4.dump`,
111,849,656 bytes. The independent watchdog execution `jobradar-health-t2c6g`
completed successfully after resuming its 15-minute scheduler. The old poll
scheduler remains paused. The Mac PostgreSQL container was stopped with its volume
retained after the cloud cycle passed.

A real reboot completed at 14:04:18 UTC. PostgreSQL, the Telegram listener, and both
systemd timers started automatically. The 14:05 timer launched discovery and a
50-board collection without any Mac services running. The listener reported
`listening for button presses`, with zero restarts and no out-of-memory exit.
The next daily backup was scheduled for October 17 at 07:21 UTC (randomized within
five minutes of 07:17 UTC).

Telegram accepted the cloud-ready confirmation as message `23`, sent from the VM
after reboot. The verification batch had no qualifying jobs; this confirms the
delivery path without relaxing the user's filters or sending an unsuitable job.
Console evidence is saved locally at
`.local/vm-migration-20261016/cloud-running.png`.

## October 19 reliability repair

`deploy/vm/apply-repairs.sh` records the guarded repair deployment. It requires a
fresh successful backup, takes the same host lock as polling/backups, retains the
old image setting in `/opt/jobradar/repairs-20261019/worker.env`, builds indexes
concurrently, refreshes planner statistics, and restarts the listener. A failed
deployment restores the old image setting; additive indexes may remain safely.
After a previous attempt it proceeds only if the retained copy still pins the
previous image and matches the live `.env` exactly; otherwise it stops for inspection.

The backup service now allows 9,000 seconds including a running scan's drain,
uses `pg_dump -Fc -Z1`, fully reads the resulting archive with `pg_restore`, and
streams it to Cloud Storage with size/checksum verification. The daily seven-slot
rotation is unchanged. A failed dump/validation leaves the prior verified local
archive intact. `backup.json` in the health bucket records the last successful
object generation and checksums; enable `JOBRADAR_CHECK_BACKUPS=1` on the independent
watchdog to alert after 36 hours without a verified backup.

Polling takes `/var/lib/jobradar/maintenance.lock` non-blockingly, so timer ticks
skip while a backup or maintenance operation holds the lock. When draining an
old poll service that predates this lock, stop the timer, wait for that cycle to
finish, then run the backup before restarting the timer. Restarting a persistent
timer immediately after asynchronously starting a backup can let a new poll take
the lock first.

See `docs/19-failed-runs-audit.md` for crash reproduction, test results, and live
repair verification. Keep the previous image and private pre-migration backup
until the new deployment has been observed through normal scheduled cycles.
