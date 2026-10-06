# Slice 4: Ashby collector

**Goal:** the third job platform. Many newer startups use Ashby (Ramp, OpenAI,
Notion, Linear and others).

**Result:** one new file (`collectors/ashby.py`) and one line in `COLLECTORS`,
like Lever. No changes to storage, dedup or the pipeline. The contract test
failed the moment Ashby was registered without a test case, which is exactly
what it's for.

## How Ashby's API differs

| | Ashby |
|---|---|
| Endpoint | `api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` |
| Payload | `{"jobs": [...]}`, all postings in one response (no paging) |
| ID | UUID |
| Location | `location` is a plain string; extra ones in `secondaryLocations` |
| Posted | `publishedAt`, ISO-8601, but the **most recent** publication time |
| Body | `descriptionHtml` (plain fallback: `descriptionPlain`) |
| Hidden jobs | `isListed: false` |
| Company name | none (comes from the registry) |
| Unknown board | HTTP 404 |

## Decisions and why

**Unlisted jobs are treated as not open.** Ashby can return postings with
`isListed: false`, reachable only by a direct link (referral-only or not yet
launched). They're dropped before normalization. A job that becomes unlisted is
therefore closed, and one that's re-listed reopens without being announced as
new. Only an explicit `false` counts. A missing or null flag means listed,
because hiding real jobs because of a missing field is the worse mistake.

**`publishedAt` is not the posting date.** Ashby documents it as the time of the
*latest* publication, so it moves forward whenever a company republishes. It's
stored as `posted_at` for reference, but "when did this job appear" is our own
`first_seen_at`, which never changes. Change detection deliberately ignores
`posted_at`: a republish with identical content isn't a change.

**Compensation is requested and kept in `raw`.** It costs nothing to ask for
(`includeCompensation=true`), and salary ranges are useful later for filtering
and ranking. `Job` doesn't model it yet, so it lives in `jobs.raw` until a slice
needs it.

**Timezone-less timestamps are UTC, not "local".** Moving date parsing into the
shared base exposed an edge case. Python reads `2026-09-30T17:12:35` (no offset)
as a *naive* datetime, and Postgres would then interpret it in the database
server's timezone, so the same posting could get different times on different
machines. The shared `parse_iso_datetime` now assumes UTC, and all three
collectors use it.

**A text-extraction fix found by real-world HTML.** Ashby wraps each list item
in a paragraph (`<li><p>BS in CS</p></li>`), which left the `-` bullet on its own
line, detached from its text. Requirements lists are exactly what the hard
filters will read ("0-2 years"), so the converter now keeps bullets and their
text together. The fix lives in the shared converter, so every source benefits,
and there's a regression test for the pattern.

**A platform migration is a repost, not a new job.** When a company moves from
Greenhouse to Ashby, its old listings disappear and the same roles reappear with
new IDs on a new host. Because the company name comes from the registry, the
level-3 fingerprint matches across platforms, and the Ashby copy is stored as
`REPOSTED` and linked to the original. An integration test covers it.

## Not done (on purpose)

- **`secondaryLocations`, `isRemote`, `workplaceType`, address country:** kept
  in `raw`. They matter for the "US only / remote OK" filter, which is the
  hard-filters slice.
- **Ashby's own `department`/`team`:** also in `raw`. They're a useful signal for
  "is this an engineering role?" in the same slice.
