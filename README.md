# jobradar

**Current runtime (October 16, 2026):** Google Cloud `jobradar-vm`, an `e2-micro`
with PostgreSQL 16 and the Telegram listener. A five-minute timer rotates through
50 boards per cycle; private backups run daily. Independent cloud health checks
run every 15 minutes. The Mac can be off: its workers and database are stopped.
Neon remains quota-blocked; no Neon upgrade was purchased. See the
[VM setup and verification](docs/18-google-vm.md) for costs, recovery, and the
limitation on reconciling Neon-only history.

A job discovery and matching pipeline for a new-grad SWE search. It polls company
career boards, persists every posting in PostgreSQL, and decides *deterministically*
what is genuinely new. AI is used only later, for matching.

**Status: the manual CLI pipeline is implemented. Original plan steps 1–4 and
6–10 are implemented, including Notion, Gmail, and manual application tracking.
Live Notion setup requires your tracker ID, property mapping, and token.
Cloud Run Jobs + Cloud Scheduler deployment and a one-cycle command are available.
Per-board polling intervals and per-host rate limiting are not implemented.**

For the older cloud deployment with Neon PostgreSQL, see the
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

The current profile sends **new Telegram alerts** for qualifying matches and retains
**one editable queue message** for browsing. Open
Telegram's **Menu** button or `/menu`, choose **🆕 New / ❤️ Liked / ✅ Applied / ⏭️ Skipped**, and tap a
job by name. Its details and **Like**, **Not interested**, **Mark applied**, and
**Sponsorship required** actions appear in the same message. **Open application**
opens the posting without marking it applied. Pagination, choices, and new
matches update the existing queue instead of adding more messages.
Use **🏠 Main menu** to switch lists and **⬅️ Back to jobs** to leave a job's details.
Jobs show their posting age, or **Updated** / **First seen** when an original
posting date is unavailable in review history. The current profile requires a
known posting date within the last **72 hours** for new and liked jobs, AI scoring,
and notifications. Each posting must explicitly offer visa sponsorship or STEM OPT
support; company history, E-Verify alone, and silence do not qualify.
Updates and discovery dates do not qualify. Applied and skipped
history is retained. Run `jobradar telegram setup-menu` once to configure
Telegram's menu shortcuts.

On macOS, `.venv/bin/python deploy/install-telegram-listener.py --with-poller`
installs the background listener and five-minute scans of up to 100 boards per cycle,
rotating least-recently attempted boards first. The public board directory is refreshed
daily. This requires the Mac, local database, and network to be available. See
[Telegram setup](docs/08-telegram.md) for credentials, limits, and operation.

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

Run `jobradar companies discover` to expand the registry from the public ATS directory.
`companies.coverage.json` records the source, counts, and unsupported platforms. The
directory supplies addresses, not sponsorship verification, and is not a complete
list of every employer. Current collectors cover Greenhouse, Ashby, Lever (including EU-hosted boards),
Workday, Workable, Recruitee, Rippling, and Personio: 12,237 directory boards.
See [public ATS collectors](docs/14-public-ats-collectors.md) for date handling,
board token formats, and coverage limits. To watch another company, add it to `companies.toml` and run
`jobradar companies sync companies.toml` again. The board is the slug in the
company's careers URL: `boards.greenhouse.io/<board>` (Greenhouse),
`jobs.lever.co/<board>` (Lever) or `jobs.ashbyhq.com/<board>` (Ashby).

Other ways to collect:

```bash
uv run jobradar collect stripe figma        # specific companies (board or name)
uv run jobradar collect --priority 1        # only the most important
uv run jobradar collect --limit 100         # rotate through least-recently polled boards
```

The October 13 matching fixes count full-time work before graduation, review hard
AI rejections against quoted requirements, invalidate outdated assessments, and
process reposts while retaining application suppression. See
[matching and coverage policy](docs/13-matching-and-coverage.md) for the rationale,
limits, and operating commands.

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
| `src/jobradar/collectors/` | `JobCollector` base (shared HTTP/error handling) and eight public ATS collectors |
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
- [x] 11. Workday, Workable, Recruitee, Rippling, and Personio public collectors.
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
