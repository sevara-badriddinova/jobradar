# Coverage, recall, and sweep I/O (Oct 21, 2026)

Follow-up to the read-only audit of the supervised worker trial. Six fixes,
implemented locally with tests; nothing is deployed.

| # | Fix | Commit | Migration |
|---|-----|--------|-----------|
| 1 | Fair board scheduling across providers; 4 h default revisit | fix1 | none |
| 2 | Sweep reads only pending rows; content rejections never rewritten | fix2 | 0013 (index) |
| 3 | Title-first detail fetching; early listing digests (Workday, Rippling, Personio) | fix3 | none |
| 4 | Sponsorship: support → AI, refusal → reject, not stated → review queue | fix4 | 0014 (table + data step) |
| 5 | Freshness by employer date *or* first sighting; initial scans never alert; repost fix | fix5 | 0015 (column) |
| 6 | 225 boards on supported ATSs (incl. Workday early-career sites) | fix6 | none (`companies sync`) |

## What changed and why

**1. Scheduler.** Providers were ordered by due *count*; with 2 slots the largest
backlogs took every slot and 3,704 boards (all Lever, Personio, Recruitee,
Rippling, 768 Workday) were never polled. Slots now alternate between the
least-recently-served provider (guaranteed rotation: every provider with due
boards within one rotation) and the most overdue board anywhere (proportional
share). Revisit is 80 min × priority (4 h for the 12k priority-3 boards ≈ 51
boards/min, inside the VM's measured 50–100/min; the old 90 min needed 136/min,
a permanent backlog).

**2. Sweep.** The incremental pending SELECT carried a `posted_at` range, so the
planner walked the date index over every recent open job (~30k heap pages per
10-minute sweep; 56% of all disk reads in the 5-minute production sample, to
decide 3 jobs). It now uses exactly the `ix_jobs_pending_active` predicate and
checks the window in Python. Title/visa/experience/location rejections are no
longer relabelled "posting date: older" as they age (thousands of rewrites a
day). `ix_jobs_expiry_open` (0013) indexes only rows the expiry UPDATEs can
change. Sweep runs hourly (every job already gets a filter task; delivery
re-checks freshness at send time).

**3. Details.** Workday/Rippling/Personio fetch one detail per posting. Details are
now fetched only for listing titles the title filter keeps (~7% of postings);
Workday and Rippling compare the listing digest before any detail request, so an
unchanged board costs only its listing pages. Big Workday boards that exhausted
the shared byte budget every poll (and were retried every minute) now fit. The
title-rule fingerprint is part of these collectors' digest, so a filter change
re-hydrates newly relevant titles. Legacy callers without a policy fetch all.

**4. Sponsorship.** `require_explicit_sponsorship = true` rejected 421 of 500 visa
rejections in 11 h only for *not mentioning* sponsorship (157 software titles;
8 of 30 benchmark jobs). Now: explicit support → normal AI pipeline; explicit
refusal → rejected; not stated (and passing every other filter, incl. location)
→ `FILTERED` with `visa: sponsorship not stated (held for review)` and an entry
in `sponsorship_reviews`. No AI call, no Telegram alert. Entries are keyed by
job fingerprint (company, title, location): reposts update the same entry and a
dismissal sticks. `jobradar sponsorship-review [--keep ID] [--dismiss ID]`;
*keep* releases the role into matching and delivery (an explicit refusal still
blocks it).

**5. Freshness.** Fresh = employer published within 72 h, **or** first seen by
JobRadar within 72 h, employer date within `max_discovery_age_days` (14), and not
found by the board's initial scan (`jobs.discovered_in_backfill`, set when a
board has no open postings yet). Undated postings stay ineligible. One rule in
SQL and Python (tested to agree on a grid), used by filters, matching, queuing
and delivery. Reposts of a role already announced or acted on are not announced
again. Bug fixed: a role re-listed under a new ID in the same complete snapshot
was classified `DUPLICATE` (its original was still open at insert time) and
hidden forever; it is now `REPOSTED`.

**6. Boards.** 222 boards mined from live SimplifyJobs new-grad software/AI listings
(supported ATS, token in the URL, not configured; Workday private/conversion
sites excluded) plus 3 verified by API (Greenhouse `sambanovasystems`, `aqr`;
Ashby `chalk`): Workday 101 (incl. KLA Ann Arbor, LSEG Graduate, HPE, Boeing
subsidiary, Cadence University sites), Ashby 37, Lever 37, Greenhouse 30,
Workable 14, Rippling 6 (225 in total). Early-career sites get priority 2.

## Expected effect

| Area | Before | After (estimate) |
|------|--------|------------------|
| Boards actually polled | 8,533 of 12,237 (70%) | all 12,462 within ~4 h |
| Supported-ATS share of 14-day Simplify US SWE/AI listings on configured boards | 42% | 63% (boards mined from the same feed: partly in-sample) |
| Benchmark (30 jobs) reachable by the pipeline | 0 new alerts | +8 via the review queue (sponsorship not stated), +up to 4 via coverage/scheduling (Mach9, SambaNova, AQR, Chalk, NVIDIA) |
| Date-blocked benchmark jobs | 8 | most stay blocked: employer dates 6–200 days old and several from initial scans; the discovery rule targets roles newly listed on known boards from now on |
| Sweep disk reads | ~240 MB / 10 min | pending rows only (tests: <50 buffers with 20k recent rejections, was proportional) |
| Workday detail requests | every fresh posting, every poll | ~7% of fresh postings, only when the listing changed |
| AI calls | unchanged for unknown sponsorship (0) | + kept reviews only (user's choice) |

## Migrations and rollback

- **0013** `ix_jobs_expiry_open` (CONCURRENTLY, re-runnable). 0012 indexes kept so
  the previous image's sweep still has its index; drop them in a later cleanup.
- **0014** `sponsorship_reviews`; data step makes the last 3 days of
  "no explicit sponsorship" rejections pending again (hundreds of rows).
- **0015** `jobs.discovered_in_backfill boolean not null default false`
  (metadata-only in PG 11+).
- Verified: upgrade 0012 → 0015 → downgrade 0012 → upgrade again on a scratch
  database; the previous release (646482b) ingests, filters and queues
  notifications unchanged on the 0015 schema (it never runs migrations itself;
  only `init-db` does).

## Rollout risks

1. **One full filter recheck.** `max_discovery_age_days` changes the filter policy
   signature, so the first sweep rechecks every active job once (a heap scan of
   `jobs`, minutes of disk on pd-standard). Run it in a quiet hour; afterwards
   sweeps are incremental again.
2. **Workday re-ingest.** Workday digests change format (listing vs hydrated), so
   each of ~1,465 Workday boards is fully ingested once (mostly comparisons, few
   writes) over the first 4 h rotation.
3. **First rotation of starved providers.** 3,704 boards (plus 225 new) get their
   first worker poll; new boards are backfill (no alerts), previously starved ones
   already have rows (their genuinely new postings can alert). Expect the review
   queue to grow by a few hundred entries in the first day.
4. **Review queue volume.** ~300+ entries/day; it is a manual list, nothing is sent.
   Dismissed roles stay dismissed across reposts.
5. **Title-first.** A role whose listing title fails the title filter but whose
   detail title would pass is now stored without details (it was rejected by
   title anyway). A title-filter change re-hydrates automatically.
6. **Queue (Mini App) delivery** still uses the strict employer-date window; alerts
   delivery (the configured mode) uses the new rule.
7. **companies sync** is a separate production step (`jobradar companies sync`).

## Unsupported providers (plan, not implemented)

Share of the 14-day Simplify US SWE/AI sample not on any supported ATS: ~33%.

| Provider | Share | Public interface | Effort | Notes |
|----------|-------|------------------|--------|-------|
| Company sites: TikTok/ByteDance | 13 | `api.lifeattiktok.com` search JSON | M | highest single-employer volume; strong new-grad program |
| iCIMS | 21 | per-tenant `/jobs/search?...&in_iframe=1` HTML, some JSON feeds | L | heterogeneous tenants; HTML parsing, pagination quirks |
| SmartRecruiters | 15 | `api.smartrecruiters.com/v1/companies/{id}/postings` (+ detail) | S | clean public JSON; best next collector |
| SuccessFactors | 13 | career-site sitemap / RSS per tenant | M | tenant discovery needed |
| Oracle Recruiting (HCM) | 9 | `/hcmRestApi/resources/latest/recruitingCEJobRequisitions` | M | public REST, per-site finder params |
| Microsoft / Amazon / Apple / Google | 12 | careers search JSON endpoints | M each | single-employer collectors; rate-limit politely |
| Eightfold, Jobvite, Paylocity, Avature, JazzHR, Breezy | ~8 | mixed (Eightfold `/api/apply/v2/jobs`, Jobvite JSON) | S–M | long tail |

Suggested order: SmartRecruiters (S, 15) → TikTok (M, 13) → Oracle (M, 9) →
big-tech sites → iCIMS/SuccessFactors. Every new collector should support
`title_first`/`early_digest` from day one and start as backfill.
