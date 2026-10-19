# Local recovery — October 16, 2026

**Historical recovery record.** Later on October 16, the verified local database
was migrated to the [Google Cloud VM](18-google-vm.md). Both local launchd workers
are now disabled and unloaded, and local PostgreSQL is stopped with its data
retained. Do not run the restart commands below while the VM owns Telegram.

JobRadar now runs against the existing local PostgreSQL database at localhost:5433.
No Neon plan upgrade or new subscription was made. Existing Anthropic API usage
continues for matching. The Mac must be awake, logged in, online, and running Docker.

## Runtime

- `com.jobradar.poll` is enabled and loaded in the user's launchd domain. It runs
  at login and every 300 seconds, discovers boards daily, rotates through up to
  100 boards, scores up to 25 eligible jobs, and sends immediate alerts and digests.
- `com.jobradar.telegram-listener` is enabled and loaded with KeepAlive for buttons.
- The existing PostgreSQL container uses `restart: unless-stopped`, also recorded
  in `compose.yaml`. This restarts the container when Docker starts; it does not
  start Docker itself or keep the Mac awake.
- After explicit user approval, both Google Cloud Scheduler jobs `jobradar-poll`
  and `jobradar-health` were paused. They must remain paused while the independent
  local worker owns Telegram updates. The cloud health job does not monitor local
  progress. No independent off-device outage monitoring is active for this setup.

The installer now calls `launchctl enable` before bootstrap, clearing the
persistent disabled overrides left by the October 13 cloud cutover.

## Data and verification

The local database was healthy at migration `0008`, with 68,158 jobs, 1,375
applications, two candidate profile versions, and 35 sent notification records.
Its previous collection ended October 13 at 15:04 UTC. A compressed backup was
saved to `.local/recovery-20261016/local-before.dump` with owner-only permissions.
This is the retained local history, not a fresh export of the inaccessible cloud
database. Cloud-only changes after the cutover have not been reconciled.

Telegram authenticated, returned no pending updates at preflight, and accepted
restored historical buttons and the queue menu using local job IDs. A live
structured-response check with the existing matching model returned successfully.

The verification cycle ran October 16, 09:07–09:08 EDT. Collection succeeded for
24 of 25 boards; `greenhouse/arcadiacareers` returned HTTP 404. Filtering, matching,
and notification stages completed with exit 0. The overall cycle returned 1 to
report that isolated board failure. No jobs in this batch met the existing filters,
and no job alerts were due. Telegram accepted recovery confirmation message 21.
The launchd poller subsequently began discovery and a new 100-board collection.

Evidence: `.local/recovery-20261016/verification-cycle.log`,
`.local/recovery-20261016/telegram-recovery.json`, and
`.local/recovery-20261016/cloud-schedules-paused.jpg`.
Ongoing logs are `.local/poll.log`, `.local/poll.err.log`, and
`.local/telegram-listener*.log`.

## Restart and future cloud cutover

Start Docker, then run:

```bash
docker compose up -d postgres
.venv/bin/python deploy/install-telegram-listener.py --with-poller
```

Before returning to cloud operation, reconcile local applications, reviews,
notification history, Telegram message mappings, and update offsets. Stop and
disable both local workers before resuming the cloud poller; separate databases
cannot share the advisory lock that protects Telegram updates. Resume cloud health
monitoring only when cloud scanning is again the intended runtime.
