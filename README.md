# jobradar

A job discovery and matching pipeline for a new-grad SWE search. It polls company
career boards, persists every posting in PostgreSQL, and decides *deterministically*
what is genuinely new. AI is used only later, for matching.

**Status: the manual CLI pipeline is implemented. Original plan steps 1–4 and
6–10 are implemented, including Notion, Gmail, and manual application tracking.
Live Notion setup requires your tracker ID, property mapping, and token.
Cloud Run Jobs + Cloud Scheduler deployment and a one-cycle command are available.
Per-board polling intervals and per-host rate limiting are not implemented.**

For unattended cloud polling with Neon PostgreSQL, follow the
[Cloud Run + Scheduler + Neon setup guide](docs/12-cloud-run.md). It includes a
container, secret storage helper, deployment script, and overlap protection.
The default scheduled cycle collects and filters; AI matching and alerts are optional.

See the [reviewed roadmap and decision rationale](docs/10-roadmap.md) for the
remaining work, correctness gaps, and proposed implementation order. The
2026-10-05 application-sync update passed all 597 tests; live external services were
not exercised.

```
GreenhouseCollector ──► Job (normalized) ──► ingest_snapshot ──► jobs table
      │ retries/backoff        │ pydantic            │ 1 transaction / board
      │ record-level isolation │                     │ L1 unique constraint
      ▼                                              │ L2 canonical URL
  collector_runs (one row per poll, ok/error/counts) │ L3 fingerprint
                                                     │ close / reopen / change detection
```

## Quick start

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
cd jobradar
docker compose up -d                        # Postgres 16 on localhost:5433
uv sync                                     # create .venv and install dependencies
uv run jobradar init-db                     # create or upgrade tables (safe to re-run)
uv run jobradar companies sync companies.toml
uv run jobradar companies list
uv run jobradar collect                     # poll every enabled company
uv run jobradar collect                     # run again: new = 0
uv run jobradar filter                      # drop senior / non-US / 5+ years / non-SWE jobs
uv run jobradar new --hours 1
uv run jobradar runs
```

Filter rules live in `profile.toml` under `[filters]`. After editing them, run
`jobradar filter --recheck` to re-apply them to jobs already filtered.

Your profile (needs an [Anthropic API key](https://console.anthropic.com)):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uv run jobradar profile extract path/to/resume.pdf   # once per resume
uv run jobradar profile show
```

Edit `[preferences]` in `profile.toml` for what your resume can't say.

Then score the jobs that passed the filters:

```bash
uv run jobradar match            # AI assessment + score, up to 25 jobs per run
uv run jobradar top              # best matches first, with reasons and missing skills
uv run jobradar match --rematch  # after changing preferences/profile (cached answers are reused)
```

Get the best matches on your phone (one-time setup: `docs/08-telegram.md`):

```bash
export TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
uv run jobradar telegram setup --test
uv run jobradar notify             # alerts for scores ≥ 80, each sent once
uv run jobradar notify --digest    # plus one digest message for 70–79
uv run jobradar telegram listen   # keep running to respond to Applied / Not interested
```

`notify` exits after sending. For buttons to respond immediately, leave
`telegram listen` running in a separate terminal. Otherwise clicks wait until
the next `notify` run and Telegram's loading indicator may time out.

Use **Add to queue** to shortlist roles, **Not interested** or **Sponsorship
required** to skip, and **Applied** after submitting. The bot shows your queue
sorted by saved match percentage when the review list is finished. **Show
queue** (or `/queue` in Telegram) opens it at any time. Controls remain visible
after a choice. Run `jobradar init-db` after upgrading, restart the listener,
then `jobradar telegram refresh-buttons` to update existing cards.

Never see a role you've already applied to (one-time setup: `docs/09-applications.md`):

```bash
export GMAIL_ADDRESS=...  GMAIL_APP_PASSWORD=...
uv run jobradar applications sync      # Notion when configured, otherwise up to 200 new emails
uv run jobradar applied 123            # or mark one by hand (ids are shown by `top`)
```

For the cheapest application sync, configure your structured Notion tracker
([setup](docs/11-notion.md)). Then `applications sync` imports Notion with zero
AI calls and skips Gmail. Without Notion configuration it reads up to 200 new
matching emails, newest first. Use `applications sync --source all` to import
Notion and also check the inbox for updates missing from the tracker. Repeating
a sync preserves source IDs and skips already-processed emails.

The loop is: `collect` → `applications sync` → `filter` → `match` → `notify`.

To watch another company, add it to `companies.toml` and run
`jobradar companies sync companies.toml` again. The board is the slug in the
company's careers URL: `boards.greenhouse.io/<board>` (Greenhouse),
`jobs.lever.co/<board>` (Lever) or `jobs.ashbyhq.com/<board>` (Ashby).

Other ways to collect:

```bash
uv run jobradar collect stripe figma        # specific companies (board or name)
uv run jobradar collect --priority 1        # only the most important
```

## Tests

```bash
uv run pytest -m unit          # ~1s, no database or network needed

# integration tests need the test database (one-time setup):
docker compose exec postgres createdb -U jobradar jobradar_test
export JOBRADAR_TEST_DATABASE_URL=postgresql+psycopg://jobradar:jobradar@localhost:5433/jobradar_test

uv run pytest                  # everything: unit + integration
uv run pytest -m integration   # only the database tests
```

| Folder | What it covers |
|---|---|
| `tests/unit/` | Pure logic with no I/O: model validation, HTML→text, dedup keys, retry/backoff, Greenhouse parsing (via a mock HTTP transport), CLI arguments and exit codes |
| `tests/integration/` | Real PostgreSQL: ingest semantics (idempotency, change detection, closing, dedup levels 1–3), schema constraints and indexes, transaction rollback, concurrent ingests, the pipeline, and end-to-end runs from Greenhouse-shaped HTTP through the CLI into the database |

Integration tests are skipped when `JOBRADAR_TEST_DATABASE_URL` is unset. They
truncate every table, so they refuse to run unless the database name ends in `_test`.
They run against real Postgres, not SQLite, because `ON CONFLICT`, partial indexes
and advisory locks are what's under test.

## Layout

| Path | Responsibility |
|---|---|
| `src/jobradar/models.py` | `Job`, the one normalized representation |
| `src/jobradar/collectors/` | `JobCollector` base (shared HTTP/error handling) + `GreenhouseCollector`, `LeverCollector`, `AshbyCollector` |
| `src/jobradar/http.py` | GET with retries, exponential backoff, jitter, `Retry-After` |
| `src/jobradar/text.py` | HTML → plain text (stdlib only) |
| `src/jobradar/dedup.py` | Pure identity keys: canonical URL, fingerprint, description hash |
| `src/jobradar/db.py` | Schema, constraints, indexes |
| `src/jobradar/repository.py` | `ingest_snapshot`: idempotent, transactional ingest |
| `src/jobradar/pipeline.py` | Runs collectors with failure isolation; records runs |
| `src/jobradar/companies.py` | Company registry: parse/validate `companies.toml`, sync it to the DB |
| `src/jobradar/filters.py` | Hard filters: title, years of experience, US location |
| `src/jobradar/settings.py` | Loads and validates `profile.toml` |
| `src/jobradar/llm.py` | The only code that calls the AI (Claude structured outputs) |
| `src/jobradar/candidate.py` | Resume → `CandidateProfile`, versioned in the database |
| `src/jobradar/matcher.py` | AI assessment per job, score computed in code, cached in `job_matches` |
| `src/jobradar/telegram.py` | Minimal Telegram Bot API client with duplicate-safe retries |
| `src/jobradar/notifications.py` | Outbox: queue → send once → buttons become feedback |
| `src/jobradar/applications.py` | Applications from any source, linked to jobs by company + same role |
| `src/jobradar/inbox.py` | Read-only Gmail with bounded batches, per-message locks, and cached classifications |
| `src/jobradar/notion.py` | Paginated, incremental Notion tracker import without AI |
| `src/jobradar/migrations/` | Alembic migrations, applied by `init-db` |
| `src/jobradar/cli.py` | `init-db`, `companies sync/list`, `collect`, `filter`, `profile extract/show`, `match`, `top`, `notify`, `telegram setup/listen`, `applications sync/list`, `applied`, `new`, `runs` |
| `profile.toml` | Your settings: `[filters]`, `[preferences]`, `[notifications]` |
| `companies.toml` | The companies you watch (edit this) |
| `docs/` | Design notes, one file per slice |

## Roadmap

These numbers preserve the original plan; they differ from the historical
slice numbers in `docs/`. Checked means implemented, not free of known gaps.

- [x] 1. Greenhouse → normalized Job → PostgreSQL → deterministic dedup (L1–L3).
- [x] 2. Company registry with priorities and enable/disable controls.
- [x] 3. Lever collector.
- [x] 4. Ashby collector.
- [ ] 5. Scheduler, persistent polling state, and per-host rate limiting.
- [x] 6. Hard filters, sponsorship checks, and Alembic migrations.
- [x] 7. Versioned candidate profile extracted from a resume.
- [x] 8. AI assessments, deterministic scoring, and context caching.
- [x] 9. Telegram alerts, digest command, outbox, and feedback buttons.
- [x] 10. Application tracking: Notion, Gmail, and manual inputs; live Notion setup still required.
- [ ] 11. Custom collectors, selected from actual coverage gaps.
- [ ] 12. Semantic repost detection (L4), pending evaluation data.
- [ ] 13. Queue/workers, deferred until measured throughput needs them.
- [ ] 14. Metrics beyond the existing collector run log.

Additional work identified by the review:

- [ ] Correct application/status handling, pending-alert eligibility, stale
  assessments, incomplete snapshots, and registry validation before automation.
- [ ] Add regression coverage for these gaps and a repeatable PostgreSQL test run.
- [ ] Add basic operational health reporting alongside the scheduler.
- [ ] Reconcile historical design notes with current behavior.

The [detailed plan](docs/10-roadmap.md) explains every proposed work item,
its reason, dependencies, and acceptance criteria. Its implementation update
marks the completed application-sync work; [Notion setup](docs/11-notion.md)
documents the new source-selection behavior.
