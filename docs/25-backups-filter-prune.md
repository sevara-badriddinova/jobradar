# Backups, title filter and stored-text prune (Oct 28, 2026)

## Findings (production, Oct 28)

| | |
|---|---|
| Nightly backups | failed Oct 23–28 (`TimeoutExpired` at 45 min); last verified backup Oct 23 18:28 UTC |
| Dump duration | 16 min (Oct 19) → 41 min (Oct 22) → over the 45-min limit |
| Each failed attempt | stopped the workers ~47 min (07:20–08:07 UTC) |
| AI budget | $1/day spent 00:02–00:11 UTC (8 PM ET); ~85 jobs scored of ~350–450 eligible a day |
| Scored jobs (Oct 26–28) | 257: 91 under 10, 17 at 70+, 6 at 80+; 1.9 AI calls per job |
| jobs table | 1.04M rows, 6.4 GB (4.7 GB TOAST); 99.8% filtered or duplicate |
| VM | 969 MB RAM, 600 MB swap in use, 35% of CPU time waiting on disk since boot |
| Collection | 33 h behind its 4-h revisit target |

## 1. Dump limit 45 → 90 min (deployed Oct 28, 13:14 UTC)

`deploy/vm/cloud_io.py`: `pg_dump` timeout 2700 → 5400 s. Copied to
`/opt/jobradar/cloud_io.py` (previous copy: `cloud_io.py.bak-20261028T131441Z`;
the file now hash-matches this repo). A manual backup was started at 13:15 UTC.
This is a stopgap: the dump grows with the table; section 3 shrinks it.

## 2. Title filter: 47 more exclusions, no full recheck

`settings.TITLE_EXCLUDE_ADDED_20261028`: field service / applications engineer,
quality / process / controls / plant / commissioning / validation engineers, chip
hardware (ASIC, FPGA, RTL, physical design, design verification, RF, optical,
photonics, analog, signal integrity), civil and utility work (construction,
structural, HVAC, facilities, substation, power systems, utility, planning
engineer), technician, CAD, presales, product support, AI trainer, talent network.

Checked against production before shipping:

| Set | Removed |
|---|---|
| Jobs waiting for AI scoring | 148 of 767 (19%) |
| Jobs ever scored | 60 of 582; highest score 45.5; 45 scored under 10 |
| Roles the user applied to | 0 of 586 |

Left out on purpose: "co op" (15 software co-op applications, one scored 90),
"design engineer" (Software Design Engineer), "project engineer", "silicon",
"production engineer" (an SRE title at some companies).

Adding exclusions can only reject more jobs, so `run_filters` recognises a
stored signature made with the previous default list
(`TITLE_EXCLUDE_BEFORE_20261028`) and, instead of a full recheck, re-applies the
title rules to open jobs that are not filtered (DISCOVERED, REPOSTED, MATCHED;
via `ix_jobs_status_first_seen`). Newly rejected jobs leave the AI queue
(`queue.drop_tasks`; leased tasks are left to their worker). Any other policy
change, or a custom list that drops a default, still runs a full recheck.

## 3. Prune the stored text of jobs that can never qualify

`jobradar prune-content` (estimate) / `--yes` (prune). A row is pruned when it is
FILTERED or DUPLICATE (not held for a sponsorship review), first seen more than
3 days ago, undated or published more than 14 days ago, and either closed or from
a collector that does not reuse stored detail payloads (Workday, Personio,
Eightfold and Oracle open postings keep theirs, or those collectors would fetch
every detail page again). Its `description` becomes `''`, `raw` becomes `{}`,
and `raw_digest` (migration 0015) keeps the MD5 of the payload as ingestion
compares it (volatile keys removed). Everything that identifies the posting
stays: IDs, title, company, location, URL, dates, status, fingerprint,
`description_hash`.

Ingestion compares a pruned row by its digest, so an unchanged posting is not
rewritten. A pruned posting that changes, reopens or gets a new date gets its
full content back before it is filtered again. The filters never judge a pruned
job (reason `content pruned: posting text no longer stored`); pruning only takes
jobs no date rule can accept, so this is reached only if the rules are widened.

Batches are short transactions that skip locked rows. Space is reusable at once;
the file shrinks only with `VACUUM FULL jobs` (exclusive lock, workers stopped).

Rollback note: the previous image ignores `raw_digest`. It would see pruned rows
that are still listed as changed and rewrite them with full content (slow, but
self-healing).

Tests: 37 new (unit and PostgreSQL integration); all 1,185 pass.
