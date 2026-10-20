# Ingestion window and the October 16–19 volume — October 19, 2026

## What happened

The directory sync on October 13 added 12,222 boards (12,237 total). As the
50-board rotation reached each new board for the first time (October 16–18),
the board's *entire* public listing was stored: 718,248 of the 736,354 rows
first seen October 16–19 (97.5%) arrived within an hour of their board's first
poll. It was a one-time backfill, not repeated ingestion: `(source,
source_job_id)` is unique and only 23 jobs reopened.

| Provider | Rows | Undated | ≤3 days old when first seen | 3–30 days | >30 days |
|---|---:|---:|---:|---:|---:|
| Workday | 478,181 | 414,892 | 38,429 | 17,667 | 6,280 |
| Greenhouse | 123,094 | 65 | 7,804 | 33,561 | 81,664 |
| Workable | 50,145 | 0 | 2,465 | 14,058 | 33,596 |
| Ashby | 44,458 | 0 | 3,977 | 13,336 | 27,113 |
| Recruitee | 16,526 | 2 | 853 | 3,921 | 11,743 |
| Personio | 8,543 | 1,270 | 293 | 1,615 | 5,363 |
| Rippling | 7,788 | 7,788 | 0 | 0 | 0 |
| Lever | 7,619 | 0 | 426 | 2,259 | 4,934 |

Workday dominates: four retail/health tenants alone contributed 64k rows
(Dollar Tree 20,145; CVS 19,780; Lowe's 13,075; TJX 11,163). Workday rows are
undated by design: details (with `startDate`) are fetched only for postings
labelled 3 days old or less; 184,189 carried "Posted 30+ Days Ago".

## Timestamp semantics per provider

| Provider | `posted_at` source | Meaning | Used to skip storage |
|---|---|---|---|
| Greenhouse | `first_published` | first publication (edits change `updated_at` only) | yes |
| Ashby | `publishedAt` | latest publication (a republish makes it recent) | yes |
| Workday | detail `startDate`; list `postedOn` label | posting start; relative label is a lower bound on age | yes (date, or label ≥ window) |
| Workable | `published_on` | publication date (date only) | yes |
| Recruitee | `published_at` | publication time | yes |
| Personio | JSON-LD `datePosted` from detail page | publication date | yes |
| Rippling | JSON-LD `datePosted` | publication date; **absent for all 7,788 rows** | n/a (undated) |
| Lever | `createdAt` | creation in the ATS — can precede publication | **no** |

`first_seen_at` is when JobRadar first stored the row; `content_changed_at`
when its content last changed; `last_seen_at` the latest poll that listed it.
None is a publication date.

## Why only one job passed

The filter's expiry sweep overwrites earlier rejection reasons with
"posting date" once a job ages past 72 hours, so current reasons overstate
date rejections. Re-evaluating a random 3,000 of the 49,572 jobs that were at
most 3 days old when first seen, with production filter code and the date
check disabled: 96.7% fail the title rules (90% "not a software engineering
role", the rest seniority), 2.3% lack explicit sponsorship (70 of the 99 SWE
titles left), 0.9% require too much experience, and none passed. The registry
is dominated by non-tech employers; among relevant roles the binding rule is
the explicit-sponsorship requirement. Only 140 of 1,375 applications link to a
stored job, so application exclusions are negligible.

## Change detection churn (fixed)

176,440 rows (158,808 undated Workday) were rewritten as "changed" within four
days. Workday's `postedOn` label ticks daily ("Posted 4 Days Ago" → "5"), so
every undated Workday posting was rewritten and re-filtered each day. Worse,
once a fresh posting's label passed 3 days its details were no longer fetched,
and the next poll overwrote the stored description and `startDate` with empty
values. Now `postedOn` is ignored for change detection, and details fetched
while a posting was fresh are reused afterwards.

## The ingestion window

`[ingestion] max_age_days` (default 30, must be ≥ `[filters]
max_posting_age_days`) skips storing a posting only when all of these hold:
it is new to the database; its provider's publication timestamp is trusted
(not Lever); and it is older than the window plus a one-day margin (time zones,
date-only feeds) — or, for undated Workday postings, labelled at least the
window ("Posted 30+ Days Ago" for 30 days). Undated, future-dated, untrusted,
and already-stored postings are always processed as before, close detection
uses every listed ID (skipping never closes anything), and no history is
deleted. Because the window is wider than the 72-hour alert window and uses
the same `posted_at`, nothing it skips could ever have been alerted.

Personio's detail page only dates a posting (title and description come from
the XML feed), so stored dates are reused; a random 1 in 12 known postings is
re-verified each poll, so a republication under the same ID is still found
within hours.

## Server-side capabilities

None of the public endpoints offers a documented date filter or newest-first
ordering: Greenhouse, Ashby, Workable, Recruitee, Personio, and Rippling return
the whole board in one response; Lever (`skip`/`limit`) and Workday CXS
(`offset`/`limit`) paginate without a documented sort guarantee. Pagination is
therefore never cut short. ETag/`If-None-Match` support was not verified and
is not relied on.

## Impact

Measured (tests, local): an unchanged Workday re-poll after a label tick writes
0 rows instead of rewriting and re-filtering every posting; a known Personio
board re-poll makes 1 request instead of 1 + up to 2 per posting.

Projected from the October 16–19 production data, had the window been active:

| Item | Projection |
|---|---|
| Rows not stored | ~350k of 736k (≈47%): 165.8k dated >30 days on trusted providers + 184.2k Workday "30+" |
| Storage | ~2.9 GB less (average stored row ≈8.2 KB incl. TOAST and indexes); `jobs` is 4.9 GB |
| Inserts avoided | ~350k rows and their index/TOAST writes |
| Daily rewrites avoided | ~44k/day (Workday label churn) plus their filter re-runs |
| Personio requests | ~11/12 of 8.5k–17k detail requests per full pass |
| List/page requests | unchanged (no server-side filtering exists) |

Steady state after the backfill: the window mainly affects newly added boards
and old postings reappearing; ongoing genuinely new postings are unaffected.

## Known gaps

- Rippling postings are always undated (JSON-LD missing), so Rippling jobs can
  never pass the 72-hour rule.
- Lever `createdAt` can precede publication; the 72-hour rule uses it too, so
  a Lever draft published long after creation is never alerted.
- Workday postings labelled 4–29 days stay undated and stored (≈230k in the
  backfill) — within the 30-day window by design.
