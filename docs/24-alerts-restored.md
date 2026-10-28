# Job alerts restored (Oct 26, 2026)

## Symptom

No Telegram job alert since October 9. Workers, listener, and Telegram delivery were
healthy (`notifications.sent_24h = 0`, no failures).

## Cause

Almost nothing reached AI scoring, so nothing could reach the 70/80 thresholds.

| Evidence (production, Oct 26) | Value |
|---|---|
| Jobs first seen in the last 3 days | 32,528: 25,601 FILTERED, 6,925 duplicates, 0 passed |
| Held as "visa: sponsorship not stated (held for review)" | 2,584 pending, 0 kept, 0 dismissed |
| Held roles still open and fresh | 433 |
| AI assessments since Oct 19 | 16 (only postings that explicitly offer sponsorship), best 67 |
| Last notification row | Oct 9 |

`require_explicit_sponsorship = true` (Oct 13) sent every posting that neither offers
nor refuses sponsorship, which is most of them, first to rejection and, from
Oct 22 (fix4), to a review queue that is reachable only with
`jobradar sponsorship-review` on the VM. Nothing in Telegram showed it.

The held list also contained clearly foreign roles ("DEU - Saxony - Dresden",
"IND - HR - NOIDA", "Prague" + "Czech Republic"). The US-only filter rejected a job
only when *every* location string was recognised as non-US, so one unrecognised
string (Workday's ISO-3 prefix, a foreign city) let it through.

## Changes

1. **Silent postings are scored** (user's choice). With
   `require_explicit_sponsorship = false`, a posting that doesn't mention sponsorship
   goes to AI scoring like any other. Alerts and digest lines say
   **🛂 Sponsorship: not mentioned in the posting**; postings that offer it quote the
   sentence. Explicit refusals, citizenship and clearance requirements are still
   rejected by the filters. The AI prompt already treats silence as not a blocker.
2. **Release of held jobs, no full recheck.** The setting is no longer part of the
   filter-policy signature (v6). Turning it off resolves pending review entries
   (`released: explicit sponsorship no longer required`) and clears `filtered_at`
   on exactly those open jobs; the next filter batch re-evaluates them (stale ones
   become "posting date: older than 3 days"). A stored v5 signature with either
   value of the setting is accepted, so this deploy does not trigger the heap scans
   and mass relabelling of a full recheck. Turning the setting back on holds new
   silent postings again, and send-time eligibility blocks unsent silent ones.
3. **US-only filter.** Workday ISO-3 prefixes (`DEU - …`, `IND - …`, `PHL …`; `USA`
   and territories are US), ~190 country names and unambiguous foreign cities
   (`geo.py`). A non-US *country* field now outweighs place names that can't be
   classified. Any US signal still wins, and generic labels ("Remote", "Hybrid",
   "2 Locations") still pass. City names that are also US places (Rome, Naples,
   Lima, Jersey…) are not used.

Tests: 30 new (unit and PostgreSQL integration); all 1,142 pass.

## Deployment (Oct 26, 13:17 UTC)

- Image `us-east1-docker.pkg.dev/jobradar-510801/jobradar/worker@sha256:38f60e1504c3f60eb9bc2c142e3a64a2cd5878d8235182d1cd4f3824a348dff6`
  (tag `alerts-20261026`). Cloud Build `78284114-5496-42f5-8a59-0e056f611962` overlays only the
  six changed modules and `profile.toml` on the previous image `sha256:5e1d0ed7…`, after checking
  that the previous image's package and profile hash-match commit `bff8612`.
- `/opt/jobradar/.env` was backed up as `.env.bak-20261026T131648Z`. The workers and listener
  were restarted. No migration was needed.
- Rollback: restore that backup, then `systemctl restart jobradar-workers` and
  `docker compose up -d --no-deps listener` in `/opt/jobradar`.

Observed after the restart:

| | |
|---|---|
| Held jobs released (the open ones) | 1,982 of 2,590 entries |
| Jobs of all 2,590 entries now: passed / too old / foreign location / closed, still labelled held | 263 / 1,994 / ~40 / 162 |
| Queued for AI scoring | 248 |
| First filter batch (release + re-evaluation, one transaction) | 13:17:55 → 13:24:09. Three ingest transactions waited on its row locks for ~4 min. |
| First job alert since Oct 9 | 13:24:37 UTC (score 80.5), plus 3 digest entries pending |

Turning the setting off again in the future repeats this one-time pass for whatever is held.
To avoid the lock wait, release held rows in their own transaction and queue them as filter
tasks instead of evaluating them in one batch. This is not implemented.

## Buttons remove decided alerts (Oct 26, follow-up)

After a choice is saved on an alert card (✅ Applied, 👎 Not interested, 📋 Add to queue,
🛂 Sponsorship required), the card is deleted from the chat. Telegram lets bots
delete their messages for 48 hours. An older card collapses to one line ("👎 Not
interested · Company — Title") with no buttons. Its notification's `message_id` is
cleared, so `telegram refresh-buttons` doesn't restore it. Digest messages stay,
because they list other jobs. In the queue message, a saved choice returns to the list
the job was opened from instead of re-showing its details. Apply ↗ only opens the
posting.

## Expected effect and cost

About 430 held roles were fresh at deploy; the location fix removes the foreign
ones, and the rest are scored at about $0.0055 each (Haiku). The existing $1/day
cap pauses matching when reached, and queued work resumes after UTC midnight
(newest first). Afterwards roughly 50–100 silent postings a day reach scoring.
Historically about 4% of scored jobs reached 80 and 15% reached 70.

## Not changed

- Collection runs about 34 hours behind its 4-hour revisit target
  (`schedule_lag_s ≈ 123,000`, ~14 boards/min on the pd-standard disk). Postings
  are found late but still qualify through the discovery rule (first seen within
  72 h, published within 14 days). A pd-balanced disk (docs/22) is the next step.
- The title filter accepts any "engineer" title, so non-software engineering roles
  (process, plant, site) reach AI scoring and score low. Tightening
  `title_must_exclude` changes the filter signature and forces a full recheck.
  That should be scheduled deliberately.
