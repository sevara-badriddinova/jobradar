# Newest postings first, 90+ alerts (Oct 29, 2026)

## Request

Alert only on matches of 90 or more. Keep the 3-day posting window, but give newer
postings priority. Apply this to new incoming jobs only: no recheck or re-score of
the jobs already in the database.

## Situation (production, Oct 29 morning)

| Measure | Value |
|---|---|
| Match queue (filtered jobs waiting for AI scoring) | ~890 |
| AI budget | $1 per UTC day, ~$0.0117 per job → ~86 jobs scored a day |
| Order of scoring | discovery order (`origin_at`), regardless of publication date |
| Budget behaviour | the whole $1 was spent in the first hour after 00:00 UTC (81 jobs on Oct 28, 86 on Oct 29, all between 00:00 and 01:00); nothing was scored for the other 23 hours |
| Scores ≥ 90 so far | 1 of 343 |

A job posted today could wait behind two days of older backlog, and anything found
after the early-morning spend waited until the next UTC midnight.

## Changes

1. **Alert threshold 90** (`profile.toml`): `immediate_min_score = 90`,
   `digest_min_score = 90`. Lower scores are stored only (`jobradar top --min-score 0`).
   Notification settings are not part of the filter signature or of the match
   context hash, so nothing is rechecked or re-scored.
2. **Newest publication day first** (`queue.claim`). Match tasks carry `rank_at`, the
   job's publication date, set when the task is queued. Matching takes the newest
   publication day first, and within a day the most recently discovered job first.
   Filter tasks keep their FIFO order. Tasks queued before this change have
   `rank_at = NULL` and are taken after every dated task, in their old order.
3. **Budget paced through the day** (`ai_budget.DailyBudgetMeter(paced=True)`). The
   $1 cap grows by 1/24 per UTC hour, cumulatively, so an idle hour's share carries
   over. A newly found job is scored within about an hour instead of waiting for
   midnight. When an hour's share is used, the worker logs
   `AI budget for this hour ($x of $1.00 a day) reached; matching resumes next hour`
   (info, not a warning). `JOBRADAR_AI_BUDGET_PACING=off` restores the old behaviour.
   Metrics include `ai.allowed_so_far_usd`.
4. **Productive boards checked more often** (`board_state.relevant_at`). A board whose
   posting passed the filters in the last 14 days is revisited every ~1 h (Workday:
   ~2 h, since its polls take ~24 s against ~2 s elsewhere), instead of the
   priority-based 1.3–4 h. Odd collection slots go first to the most overdue of these
   boards; even slots keep the least-recently-served rotation, so other boards are
   not starved. Migration 0016 seeds the flag from the boards of jobs scored in the
   last 14 days or waiting to be scored.

No filter rule changed: the filter-policy signature stays
`179fbdd2…`, so the deploy rechecked nothing.

## Rollout

- Image `worker@sha256:b71c516f7a75738ee7e9f22910c69453a8093ac5fa7b0e948512752c59e8c016`
  (Cloud Build overlay on `bad57ddd…`, smoke test: schema head 0016, thresholds
  90/90, unchanged filter signature, pacing on by default).
- Guarded deploy (Oct 29, 13:12–13:15 UTC): workers and listener stopped, database
  idle at once, migration 0015 → 0016 in under a second, restart. `.env` backup
  `.env.bak-20261029T131204Z` kept; the previous image ignores the new columns.
- Checked after the restart:
  - filter-policy signature still `179fbdd2…`: nothing rechecked;
  - 476 of 12,468 boards seeded as relevant;
  - the 917 queued match tasks: 908 old ones without `rank_at`, 9 new ones dated,
    and the dated ones are first in claim order (newest publication day first);
  - relevant boards rescheduled about 1 h after a poll (Greenhouse 54–65 min,
    Lever 55–63 min), Workday 109 min;
  - Oct 29 spend was already $1.01 (scored before the deploy), so matching stays
    paused until 00:00 UTC; paced scoring starts with the Oct 30 budget.
    Today's pause message says "for this hour" although the whole day is used: it
    applies only to spend made before pacing.

## Limits

- With $1 a day, about 86 jobs are scored daily. Newest-first means older backlog
  may never be scored. That is intended, since those jobs leave the 3-day window
  anyway.
- 90+ is rare. Expect few alerts: about 1 in 300 scored jobs so far.
- Options for more coverage, not enabled: a higher daily budget, or the Message
  Batches API (half the price per call, results within 24 hours).

## Update (Oct 29, 15:00 UTC): hourly fast lane turned off

The 14:41 UTC check showed change 4 backfired. Board polls fell from ~1,440 an hour
(08:59–13:11 UTC) to ~320 (13:21–14:48 UTC). Overdue boards grew from 6,347 to 8,364,
and the median lag went from 18 to 95 minutes. The ~550 relevant boards are the
biggest employers (238 on Workday): the slowest to fetch and ingest on this disk,
with 3x more byte-budget backpressure. Visiting them hourly crowded out the other
~12,000.

`WorkerConfig.relevant_interval` now defaults to `None`, which turns the lane off:
no shortened interval, no slot preference, no `due_relevant` query. Boards are still
marked relevant (`relevant_at`), so the lane can be turned back on later with a
smaller share. Image `worker@sha256:654f934900eb2ddb4d84181b29c70cb88847ad8bdec066ca9e75d0076b10e241`
(tag `oct29-no-fast-lane`, Cloud Build `77d351a1-c1f7-4c3b-b435-aa921e990171`).

