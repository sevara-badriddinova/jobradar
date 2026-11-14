# Haiku 5.5 only — November 14, 2026

Status: **1,270 tests pass** (Linux, Python 3.13, PostgreSQL 16). **Not deployed.**

## Why

Over the 30 days to Nov 14, AI scoring cost $10.63 for 4,672 calls. Haiku 4.5 cost
$5.84 of that (55%), even though it only re-scored the ~10% of jobs that passed the
Haiku 5.5 screen. Haiku 5.5 is priced at $0.10 / $0.50 per million input / output
tokens, against $1 / $5 for Haiku 4.5.

## Change

- `matcher.DEFAULT_MODEL` and the VM's `JOBRADAR_MATCH_MODEL` are `claude-haiku-5-5`.
  The main model now equals the screen model, so `assess()` makes one call per job.
- `llm.ask(effort=...)` sends `output_config.effort`. The matcher requests `low` for
  Haiku 5.x only (`JOBRADAR_MATCH_EFFORT` overrides; `default` sends nothing).
  Thinking counts toward `max_tokens`, so the main call allows 4,096 tokens.
- Stored assessments are keyed by `SCORING_GENERATION`, not by the model in use.
  Without this, switching models would have withdrawn and re-scored every stored
  match. The value equals the old model name, so existing hashes are unchanged.
- `cloud_io.prepare_rotated_env` also sets `JOBRADAR_MATCH_MODEL`. The deploy keeps
  runtime.env as it is, so otherwise the old model would stay until a reboot.
- Release tooling now starts from the Nov 12 release: `PRODUCTION_SOURCE = 2deb4aa`,
  and `live-baseline/` holds that release's host files.
- Alert threshold stays at 90 (`immediate_min_score`).

## Calibration (before deploying)

`deploy/vm/calibrate-haiku55.py` re-scores up to 100 jobs that have a stored Haiku
4.5 score, with Haiku 5.5 at low and medium effort. Jobs you reviewed come first.
It runs in the current production image, writes nothing, and stops at $1.50.
The output is one CSV row per job plus a table of how many jobs reach each score.
