# Jobradar reviewed roadmap and implementation decisions

Reviewed 2026-10-05. This is a repository audit and a proposed plan, not an
implementation change. The recommended order is to fix correctness gaps, make
the existing pipeline run unattended, then expand coverage and integrations.
Unchecked items remain planned. The original numbered roadmap is in the README;
the phases below are the proposed execution order.

**Implementation update, 2026-10-05:** the follow-up application-sync work now
implements the Notion adapter, cost-based source selection, bounded Gmail reads,
concurrent-message protection, atomic checkpoints, numeric role-level matching,
and order-independent rejection precedence. See [Notion setup](11-notion.md).
Live tracker configuration/verification remains pending; the other audit
findings below remain open unless explicitly checked.

**Telegram review update:** persistent queue/ignore/sponsorship decisions,
ranked end-of-list summaries, retained buttons, digest controls, application
records from Applied, and serialized update consumers are implemented.
Queue review uses existing scores and no external AI calls. Pending alerts
now recheck job status and review choices; score/tier reconciliation remains open.

## Specification and current behavior

The specifications available in this checkout are the README, `docs/01` through
`docs/09`, and the settings in `profile.toml` and `companies.toml`. No separate
original master specification was found. Historical slice notes describe the
system at different points in development, so later code and tests sometimes
supersede them.

The product is a personal US early-career software engineering job monitor:

- Normalize Greenhouse, Lever, and Ashby postings into one PostgreSQL model.
- Decide novelty deterministically, preserving duplicates, repost links,
  application history, and a separate open/closed lifecycle.
- Run inexpensive filters before AI. Ambiguous requirements should generally
  pass rather than silently hide a suitable job.
- Compare surviving jobs with a versioned resume profile and preferences.
  The model supplies judgments; code computes the score.
- Send high-scoring matches through Telegram and group the next tier into a
  digest. Preserve uncertainty after a send timeout rather than automatically
  duplicating an alert.
- Use application records to suppress roles already applied to, including
  future listings of the same role.

Current personal settings target software/backend engineering, prefer backend
and distributed systems, require sponsorship, and reject minimum experience
above **2 years**. The fallback code default is 4 years; README examples about
rejecting “5+ years” describe that fallback, not this checked-in profile.
The registry contains 15 companies. Alert thresholds are 80 immediate and 70
digest. A digest is currently sent only when `notify --digest` is invoked;
there is no daily schedule yet.

Implemented components and their original plan status are marked in the
[README roadmap](../README.md#roadmap). The manual flow is:
`collect → applications sync → filter → match → notify`.
`match` and `notify` link stored applications; they do not fetch new Gmail
messages themselves.

## Verification and limits

- [x] Read the design notes, configuration, schema, core pipeline, collectors,
  command handlers, and relevant tests.
- [x] Run the existing suite against local PostgreSQL:
  **545 passed**, comprising 397 unit tests and 148 integration tests.
- [x] Run isolated checks confirming that numeric role levels are conflated,
  an unknown top-level registry key is accepted as an empty registry, and a
  120-second `Retry-After` is reduced to 30 seconds.
- [ ] Verify live collector endpoints, account configuration, model access,
  Gmail classification quality, and Telegram delivery during operational setup.
  Existing tests use fake external services and do not establish live readiness.

The other findings below come from code inspection, not additional database
regression tests. A green existing suite does not cover those scenarios.
The project directory has no independent `.git` directory: Git currently
resolves to the user's home directory. Any future commit/CI setup must first
establish the intended repository boundary; this review did not initialize,
stage, or commit anything.

## Phase 1 Correctness before unattended operation

### 1 Application identity and state transitions

- [x] Preserve numeric seniority and meaningful technology tokens in role
  matching. Numeric levels and skills are now retained; only years and explicit
  requisition IDs are removed.
- [x] Make application status precedence independent of record order.
  Conditional database updates now preserve rejection precedence even when a
  rejection record arrives before an application record in the same run.
- [x] Route Telegram “Applied” through application recording, creating a
  source record for later same-role linking as well as updating the job.
- [ ] Guard matcher writes against a changed job status/content version.
  Writes now guard current status and skip choices. Content-version protection
  remains planned.

**Decision and reason:** keep strict role matching and one authoritative
application flow. Use conditional updates against current database state so
background work cannot erase newer user actions. Continue storing interview
and offer events in applications/feedback; new job-status values are not needed
merely to suppress an already-applied role.

**Acceptance:** tests distinguish numeric levels while still ignoring explicit
dates/requisition IDs; rejection wins in both event orders; Telegram-applied
roles suppress later matching listings; a delayed AI response cannot resurrect
an acted-on job.

Evidence: [applications.py](../src/jobradar/applications.py),
[matcher.py](../src/jobradar/matcher.py),
[notifications.py](../src/jobradar/notifications.py).

### 2 Notification eligibility and recovery

- [x] Revalidate job status and current review choice when claiming pending
  alerts or digest entries, including closed/applied/rejected/filtered/skipped roles.
- [ ] Reconcile changed scores and notification tiers before sending.
- [ ] Report existing unresolved `sending` rows on subsequent runs. The CLI
  currently reports only uncertain deliveries encountered in that invocation.
- [ ] Keep unresolved and already-notified eligible matches discoverable in
  `top`, with an explicit status/filter. It currently selects only MATCHED jobs,
  so successful notification removes a role from that view.
- [ ] Claim digest entries one message batch at a time. Today the whole digest
  is marked `sending` before its first message, so a crash can strand entries
  whose messages were never attempted.
- [x] Serialize Telegram update consumption with a PostgreSQL advisory lock;
  `listen` and `notify` cannot race the stored offset.

**Decision and reason:** retain at-most-once automatic delivery, make uncertain
outcomes visible, and keep manual resend explicit. Do not call this guaranteed
“exactly once”: the current design deliberately accepts a possible missed
message after an ambiguous network result. Rechecking eligibility protects the
central promise that acted-on jobs are not suggested again.

**Acceptance:** queue-then-apply/filter/reject/ignore cases send nothing;
score changes reconcile pending tiers; old unresolved sends remain visible;
a crash leaves unattempted digest batches pending; overlapping update consumers
cannot regress the offset. Sent alerts are never automatically requeued.

Evidence: [notifications.py](../src/jobradar/notifications.py),
[cli.py](../src/jobradar/cli.py).

### 3 Assessment freshness and complete rematching

- [ ] Refresh decision-relevant raw fields even when title, primary location,
  description, and URL are unchanged. Ingestion currently leaves `raw` stale
  in that case, including changes to secondary locations/country.
- [ ] Version filter decisions by relevant job inputs and filter settings;
  recheck affected, unacted-on jobs when either changes.
- [ ] Include the effective job inputs in match-cache identity and queue stale
  assessments for rematching. Current context hashing covers profile,
  preferences, prompt, and model, but omits job content.
- [ ] Select stale/unprocessed matching work before applying the batch limit.
  Repeated `match --rematch --limit 25` currently revisits the newest 25 MATCHED
  rows, including cache hits, and can leave older rows unvisited indefinitely.
- [ ] Recompute deterministic scores from stored assessments when scoring
  policy changes; the current cache returns its previously stored score.

**Decision and reason:** invalidate only the inputs that affect a decision,
retain historical assessments, and preserve applied/rejected history and sent
notification identity. This corrects stale recommendations while keeping the
existing cost-saving cache useful. Scoring-weight changes need code evaluation,
not another model call.

**Acceptance:** description, title, secondary-location, and filter-setting
changes invalidate the appropriate stage; unchanged jobs reuse assessments;
more than 25 stale jobs eventually all receive updated results; a scoring-only
change makes no AI call; prior applications and sends remain preserved.

Evidence: [repository.py](../src/jobradar/repository.py),
[filters.py](../src/jobradar/filters.py), [matcher.py](../src/jobradar/matcher.py).

### 4 Snapshot trust and registry validation

- [ ] Carry per-fetch completeness/parse-error information into ingestion.
  `normalize_all()` skips malformed rows while the collector's static
  `complete_snapshot` flag remains true. A previously valid posting that fails
  parsing can consequently be marked closed.
- [ ] Keep the empty-snapshot guard, but explicitly distinguish a confirmed
  empty/unlisted board from an untrustworthy response. An Ashby board whose
  last listing becomes unlisted currently stays open under the empty guard.
- [ ] Reject unknown top-level company-file keys and distinguish a deliberately
  empty registry from a missing/misspelled `company` key. Today a `compnay` typo
  becomes an empty desired registry, which sync uses to disable all entries.

**Decision and reason:** ingest valid records while withholding absence-based
closure when completeness is uncertain. Treat intentional empty boards as a
separate, tested policy. Validate the complete registry document before writes
so a typo cannot silently stop monitoring.

**Acceptance:** malformed records cannot close previously known jobs; explicit
unlisting works without making transient empty responses destructive; invalid
registry roots fail before any company is disabled.

Evidence: [collectors/base.py](../src/jobradar/collectors/base.py),
[collectors/ashby.py](../src/jobradar/collectors/ashby.py),
[repository.py](../src/jobradar/repository.py),
[companies.py](../src/jobradar/companies.py).

## Phase 2 Scheduler and basic operational health

- [ ] Add a one-cycle orchestration command and a long-running scheduler that
  call shared application functions. Keep current one-shot commands available
  for diagnosis and manual recovery.
- [ ] Add migration-backed polling state: next due time, last success, failure
  count, and ownership/lease state where needed. Make priority intervals,
  matching batch budget, digest time, and digest timezone explicit settings.
- [ ] Start with one active scheduler and sequential collection. Use a database
  lock to prevent overlapping scheduler cycles. This fits 15 configured boards
  and avoids introducing parallel ingestion before its races are addressed.
- [ ] Run collection, application sync/linking, filtering, matching, and alerts
  in dependency order; persist daily digest execution state across restarts.
  Resume due work after downtime without replaying every missed tick.
- [ ] Define stage failure behavior. A failed board should not block healthy
  boards. If a configured application sync fails, continue collection but hold
  outgoing alerts and report stale application data until recovery.
- [ ] Enforce spacing and cooldowns per host across boards, pages, and retries.
  Honor the full server-requested `Retry-After`; defer work instead of sleeping
  indefinitely when it exceeds the cycle budget. Current HTTP retries cap it
  at 30 seconds.
- [x] Add bounded inbox batches, explicit IMAP SEARCH/FETCH failure reporting,
  and per-message sync ownership. Concurrent runs now lock and recheck processed
  IDs before classification; the default budget is 200 new messages.
- [ ] Add a health command and cycle summaries showing last successful polls,
  failed stages, backlog age, stale inbox state, and pending/failed/uncertain
  notifications. Provide startup, shutdown, restart, and configuration docs.

**Decision and reason:** persist operational state in PostgreSQL, which already
owns pipeline state. A small scheduler with bounded work makes the product
automatic without requiring a broker. Serial ownership also limits duplicate
paid AI work; unique result rows alone do not prevent two external calls.
Basic health reporting moves forward from original step 14 because unattended
failures must be visible from the first scheduled run.

**Acceptance:** fake-clock tests cover priorities, cooldowns, restart recovery,
overlapping starts, and once-per-local-day digest triggering including timezone
changes/DST. Failures are visible and bounded; healthy boards continue; configured
sync failures hold alerts; no automatic replay of uncertain sends occurs.
Run PostgreSQL integration tests through a repeatable local/CI setup with an
explicit disposable test database and no silent integration skips.

**Still to choose before scheduler implementation:** actual polling intervals,
digest time/timezone, AI budget, and the machine/service that stays running.
These are product/operational settings, not requirements recoverable from the
current specification. A local scheduler stops while its host is asleep.

Evidence: [pipeline.py](../src/jobradar/pipeline.py), [http.py](../src/jobradar/http.py),
[inbox.py](../src/jobradar/inbox.py), [db.py](../src/jobradar/db.py),
[settings.py](../src/jobradar/settings.py).

## Phase 3 Complete application tracking with Notion

- [ ] Define the actual tracker data source and its company, role, status, and
  date mapping before implementing an adapter.
- [x] Initially import tracker records into `applications`, using stable
  external IDs and the existing linking logic. Persist sync checkpoints and
  report invalid/unmatched rows.
- [x] Support pagination, repeated runs, changed statuses, and partial failures.
  Keep Gmail, manual input, and Notion consistent through the same state rules.

**Decision and reason:** propose one-way import first because the existing
specification shows Notion feeding application suppression. It does not specify
two-way synchronization or writing discovered jobs back into Notion. Those
would require a separate conflict policy and explicit scope.

**Acceptance:** reimporting a tracker creates no duplicates, status edits are
reflected without losing rejection precedence, and ambiguous roles stay visible
for review rather than being guessed. Requires the tracker schema and access;
neither is present in this checkout.

## Phase 4 Expand coverage and ranking only with evidence

| Planned work | Proposed decision and reason | Completion criterion |
|---|---|---|
| Custom collectors (original step 11) | Pick a target company that the three existing collectors cannot cover; preserve normalization and completeness contracts. There is no named custom platform requirement in the repo. | Fixtures and collector-contract tests plus ingestion/closure tests for the chosen source. |
| Semantic repost detection (step 12) | Start with reviewable similarity suggestions on unresolved candidates; calibrate against labeled examples before automatically suppressing jobs. A false duplicate hides a real opportunity. | Measured false-positive behavior, explainable links, and an agreed threshold before enabling automatic decisions. |
| Queue/workers (step 13) | Defer until backlog age, cycle duration, or cost justify concurrency. Add claims/leases before parallel matching/inbox sync and coordinate dedup across boards: current advisory locks cover only one source/board. | Concurrent same-company cross-source ingestion creates one canonical role; workers recover abandoned work, prevent duplicate simultaneous AI calls, and document possible retry costs after uncertain external outcomes. |
| Extended metrics (step 14) | Build on scheduler health and `collector_runs`; add stage latency, backlog age, cache efficiency, AI usage/cost, and notification outcomes when operating the system exposes a need. | A failed or stale stage and growing backlog can be identified from recorded metrics. |
| Feedback-based ranking (mentioned in slice notes) | Retain labels now; evaluate future ranking changes against labeled examples before changing score weights or adding a learned model. This has no detailed specification yet. | Demonstrated improvement on reviewed matches without more missed eligible roles. |

## Deferred conveniences and documentation work

- [ ] Consider profile export/edit/import if extraction corrections are frequent;
  preferences notes currently provide a workaround.
- [ ] Consider OCR only if scanned resumes become an actual input requirement.
- [ ] Consider EU-hosted Lever support, automatic company discovery, salary/team
  filtering, and a jobs-to-companies foreign key when a concrete use case or
  integrity requirement calls for them. These are historical ideas, not fully
  specified deliverables.
- [ ] Reconcile older notes: migrations and multi-location filtering now exist;
  filter recheck includes MATCHED jobs; Telegram uses at-most-once delivery;
  applications include interview/offer events; configured experience is 2 years.
- [x] Complete `.env.example` with documented optional application integration/model settings
  and correct the spaced Gmail-password example so it is safe to shell-source.
- [ ] Verify provider/model configuration during live setup. Model names in code
  and mocked tests are not proof of account availability.

The next implementation slice should be **Phase 1**, with regression tests for
each specified failure case, followed by **Phase 2**. The remaining phases stay
unchecked until their prerequisites and acceptance criteria are met.
