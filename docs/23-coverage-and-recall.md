# Coverage, recall, and sweep I/O (Oct 21, 2026)

Follow-up to the read-only audit of the supervised worker trial. Six fixes,
implemented locally with tests; nothing is deployed.

| # | Fix | Commit | Migration |
|---|-----|--------|-----------|
| 1 | Fair board scheduling across providers; 4 h default revisit | fix1 | none |
| 2 | Sweep reads only pending rows; content rejections never rewritten | fix2 | none |
| 3 | Title-first details (Workday, Personio); early listing digests (Workday, Rippling) | fix3, review | none |
| 4 | Sponsorship: support → AI, refusal → reject, not stated → review queue | fix4 | 0013 (table + data step) |
| 5 | Freshness by employer date *or* first sighting; initial scans never alert; repost fix | fix5 | 0014 (column + data step) |
| 6 | 225 boards on supported ATSs (incl. Workday early-career sites) | fix6 | none (`companies sync`) |

## What changed and why

**1. Scheduler.** Providers were ordered by due *count*; with 2 slots the largest
backlogs took every slot and 3,704 boards (all Lever, Personio, Recruitee,
Rippling, 768 Workday) were never polled. Slots now alternate between the
least-recently-served provider (guaranteed rotation: every provider with due
boards within one rotation) and the most overdue board anywhere (proportional
share). Overdue age in the oldest-board slot is capped at one priority-3
revisit, so never-polled boards share it after a deploy. Revisit is 80 min × priority (4 h for the 12k priority-3 boards ≈ 51
boards/min, inside the VM's measured 50–100/min; the old 90 min needed 136/min,
a permanent backlog).

**2. Sweep.** The incremental pending SELECT carried a `posted_at` range, so the
planner walked the date index over every recent open job (~30k heap pages per
10-minute sweep; 56% of all disk reads in the 5-minute production sample, to
decide 3 jobs). It now uses exactly the `ix_jobs_pending_active` predicate and
checks the window in Python. Title/visa/experience/location rejections are no
longer relabelled "posting date: older" as they age (thousands of rewrites a
day). The remaining expiry candidates are a subset of the existing
`ix_jobs_expiry_active` predicate (0012, 1.5 MB in production), so no new index
is needed. Sweep runs hourly (every job already gets a filter task; delivery
re-checks freshness at send time).

**3. Details.** Workday/Rippling/Personio fetch one detail per posting. Workday
and Personio now fetch details only for listing titles the title filter keeps
(~7% of postings), reusing stored details otherwise; Workday and Rippling compare
the listing digest before any detail request, so an unchanged board costs only
its listing pages. Rippling keeps fetching every detail when its listing changed:
it has no stored-detail reuse, so skipping would replace an existing posting's
description and date with listing-only fields. Big Workday boards that exhausted
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

- No new index: an earlier draft's 0013 index would have cost two full scans of
  `jobs` (~8 min of saturated disk) at deploy for no planner benefit.
- **0013** `sponsorship_reviews`; data step makes the last 3 days of
  "no explicit sponsorship" rejections pending again (hundreds of rows).
- **0014** `jobs.discovered_in_backfill boolean not null default false`
  (metadata-only in PG 11+); data step re-queues open "too old" rejections first
  seen in the last 3 days with an employer date within 30 days.
- Verified: upgrade 0012 → 0014 → downgrade 0012 → upgrade again on a scratch
  database; the previous release (646482b) ingests, filters and queues
  notifications unchanged on the 0014 schema (it never runs migrations itself;
  only `init-db` does).

## Rollout risks

1. **No full filter recheck.** The filter policy signature excludes the discovery
   window (verified: old release + old profile and new release + new profile give
   the same signature), so deploying does not trigger heap scans of `jobs`. 0013
   and 0014 re-queue only the recent rows the new rules can change.
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
6. **Mini App** uses the same freshness rule as filtering and alerts (the
   listener and `notify` pass the discovery window into the queue state).
7. **companies sync** is a separate production step (`jobradar companies sync`).
8. **Image downgrade after the big-company boards are synced.** The automatic
   rollback (trial guard, `workers.sh rollback`) runs the legacy poll on the
   *same* image, which knows the new collectors. Reverting to an *older image*
   is different: its `poll` builds every enabled board up front and fails on an
   unknown collector (`amazon`, `eightfold`, `oracle`). Before starting an older
   image's poll, run that image's `companies sync companies.toml` once: its
   baked registry does not list the new boards, so sync disables them.

## Big-company career sites (Oct 21, 2026)

Coverage of the 16 requested employers, checked against `companies.toml`
before adding anything:

| Employer | Status | How |
|----------|--------|-----|
| NVIDIA, Salesforce (4 sites), Walmart, Cisco, Fidelity, Bank of America | already covered | Workday |
| Amazon | **new collector** `amazon` | amazon.jobs `search.json`, category `software-development`, USA (1,742 open) |
| Microsoft | **new collector** `eightfold` | PCSX search, US + profession "software engineering" (331 open) |
| Netflix | **new** `eightfold` (v2 mode) | `/api/apply/v2/jobs` (PCSX search returns 403 for this tenant), ~380 open |
| Oracle | **new collector** `oracle` | Oracle Recruiting Cloud REST, site CX_45001, US (1,968 open) |
| JPMorgan Chase | **new** `oracle` | same API, site CX_1001, US, categories Software Engineering (643) and Infrastructure Engineering |
| Google, Apple, Meta, TikTok, IBM | gap | see below |

Design, shared by the three collectors:

- **Complete snapshots of a filtered slice** (category / profession / country
  facets, never a keyword query), so a posting missing from a complete poll is
  closed. Live, date-sorted offset pagination overlaps consecutive pages (10 of
  100/200; 1 of 10 for Eightfold) and de-duplicates by ID, absorbing removals
  between requests. A failed page or detail, fewer unique postings than the
  reported total, or a total beyond the source's offset cap (Amazon 10,000)
  marks the snapshot incomplete: jobs are ingested, nothing is closed.
- **Title-first, early digest, detail reuse** (Oracle, Eightfold): the listing
  digest is checked before any detail request; details are fetched only for
  titles the filter keeps and listings newer than 30 days, and stored details
  are reused (`reuses_known_details = "detail"`). Amazon needs no details (the
  search carries description and qualifications).
- **Dates:** Amazon `posted_date` (calendar day, midnight UTC; the 1-day age
  margin absorbs it); Oracle `ExternalPostedStartDate` (exact) else the listing
  `PostedDate`; Eightfold `postedTs`, except when it is more than 45 days after
  `creationTs` (Netflix re-posts long-open requisitions with a fresh `postedTs`),
  then the creation time, so a re-post never looks fresh. Unparseable dates
  are undated and never fresh. Oracle postings past `ExternalPostedEndDate` /
  `PostingEndDate` (inclusive of the last day) are excluded, i.e. closed.
- **Volatile fields stripped** before digest and storage (Amazon "updated N
  hours ago", team images; Eightfold relevance scores), so unchanged boards stay
  unchanged.
- **IDs namespaced by board** (`source_job_id = board:id`): two slices of one
  tenant never claim the same row; cross-board/cross-source duplicates still go
  through fingerprint dedup.
- **Policy unchanged:** the jobs enter the same filters (posting date, title,
  experience, explicit sponsorship / review queue, location) and the same
  $1/day AI meter. Compact location projections were added to
  `query_fields.location_raw` (without them the filters would have seen no
  structured locations for these sources); a test now requires a projection
  for every collector.

Load per rotation (priority 1, every 80 min): Amazon ~20 requests / ~20 MB
held in the 64 MB byte budget (only one Amazon board); Microsoft ~37 listing
requests; Netflix ~43; Oracle ~11; JPMorgan ~5. Detail requests only for new,
relevant, recent postings (first poll: a few hundred across all five; then a
handful per rotation). New boards start as backfill (no alerts for the
existing stock). Expected AI volume: the first-day backfill does not reach AI;
afterwards roughly 10–30 fresh relevant postings/day from these employers,
of which those with explicit sponsorship language reach matching — well inside
$1/day (the meter pauses matching, never drops tasks).

Gaps (not implemented; no public unauthenticated JSON we can use responsibly):

| Employer | Interface | Blocker / plan |
|----------|-----------|----------------|
| Google | careers.google.com renders server-side; no documented JSON | HTML parsing of results pages; fragile, M–L |
| Apple | jobs.apple.com search API needs a CSRF token from the page session | token handshake per poll, M |
| Meta | metacareers.com GraphQL with per-session tokens (`lsd`, doc IDs) | brittle, L; not recommended |
| TikTok / ByteDance | `api.lifeattiktok.com` search requires signed headers | M; highest single-employer volume, next after SmartRecruiters |
| IBM | careers.ibm.com search backend (Avature-style) | needs verification, M |
| SmartRecruiters (ATS) | `api.smartrecruiters.com/v1/companies/{id}/postings` | S; could not be re-verified today (robots-disallowed for the fetch tool, browser bridge offline) |
| JPMorgan / BofA early-career sites | separate campus portals | BofA campus roles appear on its Workday site; JPM campus program pages are not job feeds |

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
