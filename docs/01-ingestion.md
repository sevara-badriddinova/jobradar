# Slice 1: ingestion, idempotency and dedup

This slice answers one question reliably: **is this posting new?** Nothing
downstream (filters, AI, notifications) can be trusted until that answer is right.
These notes explain each design decision and the concept behind it.

---

## 1. Normalization: one internal model

Every source returns a different shape. Greenhouse escapes its HTML, uses
`first_published`, and puts the location in `location.name`. Lever and Ashby
each do something else. The collector's only job is to turn that into a `Job`.

**Why:** with N sources and M downstream stages, normalizing at the edge means
N + M pieces of code instead of N × M. The dedup code in this slice will work
unchanged for Lever and Ashby.

The raw payload is stored too (`jobs.raw`, JSONB). If the normalizer has a bug,
it can be fixed and re-run over stored data without re-fetching from the source.

## 2. The database decides what is new, not the application

`UNIQUE (source, source_job_id)` is the core idempotency guarantee. New rows are
inserted with `INSERT … ON CONFLICT DO NOTHING RETURNING id`:

- a returned id means this call created the row, so the posting is new;
- no id means it already existed, so it is not new.

**Why not "SELECT, then INSERT if missing" in Python?** Two overlapping runs can
both see "missing" and both insert. That's a check-then-act race. A uniqueness
constraint makes the database the arbiter, so correctness doesn't depend on
there only ever being one process. That matters when this becomes workers
(step 13).

**Idempotency** means applying an operation twice has the same effect as
applying it once. Here, re-polling a board any number of times leaves the same
rows and reports `new = 0`. That's what makes retries safe. When something
fails, just run it again.

## 3. One transaction per board snapshot

`ingest_snapshot` processes a whole board inside one transaction. If anything
fails halfway, it all rolls back and the next poll redoes it. There's never a
partially ingested board.

It also takes `pg_advisory_xact_lock(hash(source:board))`. Two ingests of the
*same* board are serialized, so the level-2/3 lookups can't race. Different
boards still run fully in parallel. The lock is released automatically at
commit or rollback, so it can't leak.

## 4. Multi-level deduplication

Cheapest and most certain first. Fuzzier levels only see what earlier levels
didn't resolve.

| Level | Key | Catches | Result |
|---|---|---|---|
| 1 | `(source, source_job_id)` | Same posting re-polled | update `last_seen_at` |
| 2 | `canonical_url` | Same posting via another ID or source; tracking params like `gh_src`, `utm_*` | stored as `DUPLICATE`, linked |
| 3 | `fingerprint` = sha256(company, title, location) | Same role re-listed under a new ID | `REPOSTED` if the original closed, else `DUPLICATE` |
| 4 | embeddings (later) | Same role with a reworded title or description | step 12 |

Duplicates are **stored, not dropped**, with `duplicate_of_id` pointing to the
root (canonical) job. You keep an audit trail, and when Gmail sync later marks
the root `APPLIED`, every copy is covered by following that link.

Canonical URLs strip *tracking* params but keep *identifying* ones. Greenhouse
career pages look like `stripe.com/jobs/search?gh_jid=8172508`. Dropping
`gh_jid` would make every Stripe job look identical.

**A known tradeoff at level 3:** companies sometimes post two real openings
with the same title and location. Today the second is treated as a duplicate.
For one job seeker that's the right bias: you'd apply once anyway. Level 4 can
refine this later.

## 5. Change detection and closing

- **Seen again:** `last_seen_at` is bumped in one bulk `UPDATE`, not one per row.
- **Content changed:** if the description hash, title, location or URL differs,
  the row is updated and `content_changed_at` is set. It's *not* new. A later
  step can decide whether a material change should trigger re-matching.
- **Missing from a complete snapshot:** `closed_at` is set. A Greenhouse
  response contains every open job, so absence means it was taken down. Each
  collector declares `complete_snapshot`. A collector that paginates or searches
  and might miss postings must set it to `False`, and then nothing is closed.
- **Reappears:** `closed_at` is cleared. It's *not* new.

**Safety rails:** an empty response never mass-closes a board. A response where
*no* records parse is treated as a failed fetch, not as an empty board. Both are
far more likely to be an API hiccup or a format change than a company closing
everything at once.

**Why `closed_at` instead of a `CLOSED` status?** Pipeline status (what *you*
did: `NOTIFIED`, `APPLIED`) and lifecycle (whether the *posting* is still up)
are independent. A job you applied to can close. If closing overwrote `status`,
the `APPLIED` fact would be lost, and a repost could then be shown to you
again. Keeping them separate prevents that by construction.

## 6. Failure isolation and retries

At three levels:

1. **Request:** transient failures (timeouts, 429, 5xx) are retried with
   exponential backoff and *full jitter*, and the server's `Retry-After` is
   honored. Non-retryable errors (404 for a bad board token) fail immediately.
   Retrying them only adds load and latency.
2. **Record:** one malformed posting is logged and skipped. The rest of the
   board is still ingested.
3. **Board:** `run_collector` catches everything at its boundary. One broken
   board is recorded as a failed run, and the next board runs normally.

Jitter matters because many boards failing together (say, a network blip)
would otherwise all retry at the same instant. That's a thundering herd.

## 7. Observability, minimal version

Every poll writes a `collector_runs` row with ok/error and the counts (fetched,
new, seen, changed, duplicates, closed, reopened). `jobradar runs` shows them.
Silent failure is the worst failure mode for a monitoring system. If a board
has failed for two days, you need to be able to see that. This table also
becomes the data source for metrics in step 14.

## 8. Indexes follow queries

| Index | Serves |
|---|---|
| `uq_jobs_source_job` | level-1 lookups and the conflict target |
| `ix_jobs_canonical_url` | level-2 lookup |
| `ix_jobs_fingerprint` | level-3 lookup |
| `ix_jobs_open_by_board` (partial: `closed_at IS NULL`) | close detection only scans open jobs |
| `ix_jobs_status_first_seen` | downstream stages pulling `status = 'DISCOVERED'` work |

That last index points at the next design move. Later stages (filters, matcher)
will **read their work from the database by status**, not receive an in-memory
list from the collector. If the matcher crashes, nothing is lost. The rows are
still `DISCOVERED` and get picked up on the next run. This is the same
reasoning that later justifies a queue.

## What's deliberately not here yet

- **Alembic migrations.** `create_all` is fine until there's data worth migrating.
- **Async/concurrency.** Polling a handful of boards sequentially takes seconds.
  Concurrency arrives with the scheduler, together with per-host rate limits,
  so the two are designed together.
- **Company registry.** Boards are passed on the command line for now. That's step 2.
