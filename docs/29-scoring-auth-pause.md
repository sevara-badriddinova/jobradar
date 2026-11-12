# Rejected API key: pause, alert, recover — November 11, 2026

Status: **1,257 tests pass** (Linux, Python 3.13, PostgreSQL 16). **Not deployed**; ships
with the docs/28 overlay, which touches the same files.

## What happened

From at least November 9 the VM's Anthropic key returned `401 invalid x-api-key`.
The matcher treated that like any job failure: every match task spent its attempts
and died (1,058 dead, 216 waiting on Nov 11), nothing was scored, no alert was sent
in 24 hours, and nothing said why.

Hotfix on the VM, Nov 11 12:09 ET: the new key is version **3** of
`jobradar-anthropic-key` (versions 1 and 2 are disabled). `/opt/jobradar/cloud_io.py`
reads version 3 for the Anthropic key only; Telegram secrets stay on version 1.
The original is `/opt/jobradar/cloud_io.py.bak-20261111`. This repo's `cloud_io.py`
does not carry the hotfix: **when applying docs/28, pass Anthropic version 3.**

## Change

- `llm.CredentialsRejected` (a subclass of `LLMError`, so other callers keep their
  messages) for 401, 403, an exhausted credit balance, and a missing key. Its
  `reason` is short and safe to show in an alert.
- The matcher re-raises it; no job is marked failed.
- The worker releases the claimed tasks with **no attempt charged**, pauses matching
  for `auth_recheck_interval` (10 min), then tries one batch again (a 401 is free).
- One Telegram message when scoring goes down and one when a real assessment
  succeeds again. The open alert is the `scoring_down_alert` row in `app_state`, so
  restarts and parallel workers never repeat it. If Telegram certainly did not get
  the message, the row is released and the next pause retries.
- Visible state: runtime `matching_auth_paused`, stage state `auth_paused`, metrics
  `ai.auth_paused` and `ai.auth_error`. The watchdog reports
  "The AI provider is rejecting the API key".
- `jobradar match` exits 2 with a clear message instead of a traceback.
- `jobradar requeue match --dead [--dry-run]` revives dead match tasks whose jobs
  are open, filtered, unreviewed, unscored and inside the freshness window
  (`max_posting_age_days`, `max_discovery_age_days`). Older ones stay dead; matching
  would skip them anyway. Plain `requeue match` still queues every eligible job.

## VM hotfixes, now in the repo (Nov 12)

Two changes were made by hand on the VM. Both are now in `deploy/vm/cloud_io.py`,
so the release keeps them:

1. **Anthropic key version 3.** The hotfix pins it in the VM's `cloud_io.py`. The
   release instead records it in `/opt/jobradar/anthropic-secret-version`
   (apply argument `3`).
2. **$1 daily AI budget.** `configure` rewrites `runtime.env` on every stack start,
   so the hand-added `JOBRADAR_AI_DAILY_BUDGET_USD` line was lost on Nov 11 and
   matching ran uncapped for a day. The value is now part of `configure`.

`deploy/vm/live-baseline/` holds the VM's host files exactly as they are now
(`b40bb90` plus those two hotfixes). The apply script refuses to run if the VM
differs from them.

## Release (docs/28 + this change)

From a full clone of the release branch in Cloud Shell:

```sh
bash deploy/vm/cloudshell-release.sh check    # VM == live-baseline; image == b40bb90 source
bash deploy/vm/cloudshell-release.sh build    # package, build overlay, push, test the pushed digest
bash deploy/vm/cloudshell-release.sh apply    # guarded apply; restores the old setup on failure
bash deploy/vm/cloudshell-release.sh verify
```

Release fixes found while preparing it:
- The image overlay is now every file under `src/jobradar` changed since the
  production source (`PRODUCTION_SOURCE` in `package-reliability.py`). The old
  hand-kept list would have shipped the new `worker.py` without `llm.py` and
  `matcher.py`.
- Test evidence also records the pushed digest reference (line 4). Image IDs differ
  between Docker's classic and containerd image stores, and the VM uses containerd,
  so the ID check alone would refuse a correctly tested image.

## After deploying

```sh
cd /opt/jobradar
sudo docker compose -f compose.yaml -f compose.workers.yaml run --rm --no-deps -T worker \
  requeue match --dead --dry-run
```

Drop `--dry-run` to apply. The alert path is covered by tests; don't revoke the live
key to try it.

## Tests

`tests/integration/test_auth_pause.py` (pause keeps every task, one alert across a
restart, one recovery message after a restart, no message when nothing was down,
retry after a certainly-undelivered alert, `requeue --dead` scope),
`tests/unit/test_llm.py` (error mapping) and `tests/unit/test_health.py`
(watchdog message). Without the matcher change, three of the integration tests fail.
