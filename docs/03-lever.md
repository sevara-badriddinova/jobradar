# Slice 3: Lever collector

**Goal:** a second job platform, and a test of the collector abstraction. If
adding Lever needed changes to dedup, storage or the pipeline, the boundary
would be in the wrong place.

**Result:** one new file (`collectors/lever.py`), one line in `COLLECTORS`, and
no change to `repository.py`, `db.py` or `pipeline.py`. Lever jobs get the same
idempotency, change detection, close detection and dedup as Greenhouse for free.

## Why each platform is its own collector

Each one has its own API: URL, response shape, field names, ID format,
timestamp format and paging. The collector's job is to absorb those differences
and output the same `Job`.

| | Greenhouse | Lever |
|---|---|---|
| Endpoint | `boards-api.greenhouse.io/v1/boards/{b}/jobs?content=true` | `api.lever.co/v0/postings/{b}?mode=json` |
| Payload | `{"jobs": [...]}` | bare array `[...]` |
| ID | integer | UUID |
| Title | `title` | `text` |
| Location | `location.name` | `categories.location` (+ `allLocations`) |
| Posted | ISO-8601 string | epoch **milliseconds** (`createdAt`) |
| Body | one escaped-HTML blob | intro + titled `lists` + closing `additional` |
| Company name | `company_name` | none (comes from the registry) |
| Paging | none: one response is everything | `skip` / `limit` |

## Decisions and why

**Shared behaviour moved into the base class.** Owning the HTTP client,
turning fetch failures into `CollectorError`, and skipping malformed records
(while refusing an all-malformed response) were written inside the Greenhouse
collector. A second collector would have copied them, and copies drift. They now
live once in `JobCollector`. That's the "rule of three" applied a little early,
because the second copy here would have been identical.

**Explicit pagination, never trusting a default.** Lever's docs don't say what
happens when `limit` is left out. Relying on an undocumented default means a
company with 150 jobs might silently return 100. Then the other 50 get marked
*closed*, and get re-announced as reopened on the next poll. So the collector
always asks for pages of 100 and stops on a short page.

**Overlapping pages.** Offset pagination over a list that changes isn't a
consistent snapshot. If a posting is removed between the request for page 1 and
page 2, everything after it shifts left by one, and one posting falls into the
gap between the pages. Each page therefore starts 10 earlier than the last one
ended, and results are de-duplicated by ID. That absorbs up to 10 removals
during a poll for the cost of a few repeated rows. (A unit test simulates
exactly this removal, and fails if the overlap is removed.)

**Partial results are an error, never a smaller snapshot.** If paging runs past
50 pages, which no real company has, the collector raises instead of returning
what it has. Returning it would close every posting it didn't reach. The same
rule as an all-malformed response: when unsure, fail the run and change nothing.

**Rebuilding the description.** Lever splits a posting into an intro, titled
lists ("What you'll do", "Requirements") and a closing section. They're joined
into one plain-text document with the headings kept, because the requirements
list is exactly where "0-2 years" or "5+ years" lives, and the hard filters and
the AI matcher need it. Plain-text fields are used only when the HTML is empty.

**Raw payload kept.** Lever includes `salaryRange`, `workplaceType`, `country`
and `allLocations`, which `Job` doesn't model yet. They're kept in `jobs.raw`, so
the hard-filter slice (e.g. "US only") can use them without re-fetching anything.

**A contract test for all collectors.** `tests/unit/test_collector_contract.py`
runs the same checks against every collector in `COLLECTORS`: identity,
normalized output, deterministic results, a 404 becoming a `CollectorError`,
retrying transient errors, and rejecting garbage. A test fails if a collector is
registered without a contract case, so Ashby can't skip it.

**Same company on two platforms.** Companies migrating between platforms often
list a role on both for a while. Because the registry name is used as the
company everywhere, the Greenhouse and Lever copies get the same fingerprint,
and the second is stored as a `DUPLICATE` of the first. An integration test covers it.

## Not done (on purpose)

- **EU-hosted Lever sites** (`api.eu.lever.co`): `api_base` is a class
  attribute, so it's a three-line subclass when a company on your list needs it.
- **Multi-location postings** use Lever's primary `categories.location`. The
  full `allLocations` list is in `raw` for the location filter to use later.
- **The `collect` table** now prints `source/board`, because the same slug can
  exist on two platforms.
