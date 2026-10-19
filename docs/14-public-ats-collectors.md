# Public ATS collectors

The registry supports all eight platforms in the imported public directory. The
October 13, 2026 expansion adds 6,012 board addresses: Workday 1,364, Workable
1,875, Recruitee 1,043, Rippling 864, and Personio 866. The complete registry has
12,237 addresses. These are boards, not necessarily distinct employers; registration
means they enter the polling rotation, not that every address has responded.

## Endpoints and publication dates

| Platform | Public source | Publication field |
| --- | --- | --- |
| Workday | CXS POST search, then GET posting detail | `jobPostingInfo.startDate` |
| Workable | Widget accounts API with `details=true` | `published_on` |
| Recruitee | Careers Site API `/api/offers/` | `published_at` |
| Rippling | Public board jobs API and individual detail/page | `JobPosting.datePosted` |
| Personio | English XML feed and individual public page | `JobPosting.datePosted` |

Creation dates (`createdOn`, `createdAt`, `created_at`), update dates, discovery
times, and relative labels never substitute for publication dates. Missing dates
remain ineligible under the maximum 72-hour age policy. Date-only values use
midnight UTC, conservatively excluding jobs that might already be too old.

Workday's English labels older than three days avoid unnecessary detail requests;
those rows retain their identity with an unknown publication date and cannot
notify. Recent or unrecognized/localized labels always require an absolute date
from the detail endpoint. The original page total controls pagination because
some Workday boards return zero totals on subsequent pages. Repeated or truncated
pages retain valid earlier results with `complete_snapshot=False`. Malformed
entries are skipped while pagination advances by the original page length.
Incomplete listings can add/update valid jobs but cannot close missing jobs;
a completely unusable result still fails the board.

Workday's case-sensitive site names are hex encoded in the lowercase registry
key: `wd1:acme:45787465726e616c` represents tenant `acme`, site `External`.
Discovery builds this automatically. IDs include the board to prevent identical
requisition numbers at unrelated employers colliding. Personio supports both
`.de` (bare slug) and `.com` (`com:slug`) domains. When the English detail page
is unavailable or lacks a valid `datePosted`, Personio retries the same job in
the default language. Its English XML description remains the matching input.
The fallback never uses `createdAt` as a publication date.

## Safety and delivery

All endpoints are public reads. Workday's POST is a read-only job search. Requests
share retries and timeouts. Detail requests use at most four workers per board;
boards still run sequentially, keeping ingestion and deduplication serialized.
A failed detail or malformed normalized record marks the snapshot incomplete,
preventing false closures. A completely unusable response fails the board.

Descriptions and structured secondary locations enter the existing filters,
matching, reassessment, repost, and Telegram outbox pipeline. The strict
role-specific sponsorship requirement is unchanged and is checked again before
sending. Unknown dates and sponsorship do not qualify. No thresholds are lowered
to force alerts. Export-license sponsorship is excluded from visa-refusal
matching only when it is the immediate object of that particular statement.
Separate visa refusals and citizenship/clearance restrictions still apply.

Directory refresh validates vendor hosts and board paths; CSV `api_url` values
are not executed. A newly supported platform invalidates the daily discovery
cache. Existing manual names, priorities, and disabled boards survive imports.

## Verification and limits

Contract tests cover every collector. Additional tests cover Workday pagination,
site case and identity, secondary locations, date provenance, partial failures,
XML entity rejection, URL validation, and retrying read-only searches. Integration
tests take each new source through filtering, matching, and fake Telegram delivery
exactly once under the three-day and explicit-sponsorship policy.

Representative live API checks are recorded locally in
`.local/new-collector-live-results.json`. They are sample checks, not an audit of
all 12,237 boards. Some public Rippling/Personio postings expose no publication
metadata, and inactive boards may fail. The run log records individual failures.
Recruitee currently documents an authentication requirement starting February 10,
2027; future API access changes may require another public-feed adapter.

The local poller rotates 100 boards per cycle, starting every five minutes when
idle. A 12,237-board rotation requires at least 123 cycles (about 10.25 hours);
long collections, Mac sleep, and network failures extend this. No always-on or
complete-internet coverage guarantee is implied.

Vendor references:

- [Workable public careers endpoints](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page)
- [Recruitee Careers Site authentication roadmap](https://docs.recruitee.com/reference/authentication-1)
- [Personio XML feed](https://support.personio.de/hc/en-us/articles/207576365-Integrate-jobs-from-Personio-into-your-website-via-XML)

## October 13 repair verification

The targeted repair scan is recorded in `.local/audits/repair-rescan-results.json`,
with pre-scan targets and date counts in `.local/audits/repair-baseline.json`.
Tests cover malformed Workday list entries, truncated/repeated/failed pages,
preservation of existing open jobs, Personio default-language dates, and export
license wording alongside real immigration restrictions.

Rippling investigation found `jsonLd: null` in all 478 records present at the
start of the repair. The public page's `jobPost` data and rendering code also
use that field for structured metadata; `createdOn` is the only date field
observed, with no evidence that it represents publication. The collector checks
the public page even if API JobPosting metadata exists but lacks a valid date.
If neither exposes `datePosted`, the posting remains undated and ineligible;
creation or discovery time is not substituted.

The completed targeted repair rescan processed 12,124 records across 70 boards:
all returned usable results, and six Workday snapshots were incomplete with close
detection disabled. Cloudflare was refreshed separately (436 postings). Personio
recovered 610 publication dates, bringing its known dates from 71 to 681 and its
postings within 72 hours from 1 to 32 at verification time. All 478 refreshed
Rippling postings still lacked verified publication dates. The 840-test suite
passed. Final strict-policy filtering produced no eligible jobs, so no new AI
assessments or Telegram alerts were needed. See
`.local/audits/notification-audit-after-repair.json` for the final live snapshot.
