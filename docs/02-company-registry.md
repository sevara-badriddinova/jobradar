# Slice 2: company registry

**Goal:** the list of companies to watch becomes data, not something you
retype on every command. The scheduler (slice 3) needs exactly this: a list of
boards with priorities.

```
companies.toml ──(companies sync)──► companies table ──► collect ──► collectors
  (you edit, in git)                   (what the app reads)
```

## How it works

- `companies.toml` lists each company: name, collector, board, priority, enabled.
- `jobradar companies sync companies.toml` makes the `companies` table match the
  file: it adds new entries, updates changed ones and disables removed ones.
  `--dry-run` shows the changes without saving them.
- `jobradar collect` polls every enabled company. `collect stripe figma` polls
  just those, and `collect --priority 1` only the most important.
- The collector for each row is looked up in a registry of implementations
  (`COLLECTORS` in `collectors/__init__.py`). Adding Lever later is one class
  plus one line there.

## Decisions and why

**A file you edit + a table the app reads ("declarative config").**
The file is easy to edit, review and keep in git, and it's the source of truth
for *what you want*. The table is what the code reads, because the next slice
stores runtime state on each company (when it was last polled, how many
failures in a row), and that state must live in the database. `sync` is the
one-way bridge from file to table, the same pattern as Terraform or Kubernetes
manifests: you declare the desired state, and the tool reconciles toward it.

**Sync is idempotent.** Running it twice changes nothing the second time and
says so ("registry already in sync"). Safe to run on every deploy.

**Validate everything, then write all or nothing.** The whole file is checked
first (unknown collector, bad priority, typo'd keys, duplicate boards, a full
URL where a slug belongs) and *every* error is reported at once. If anything is
invalid nothing is written, because a half-applied registry is worse than none.
`extra="forbid"` turns a typo like `prority = 1` into an error rather than a
silently ignored setting.

**Removing a company disables it; it doesn't delete it.** Its jobs, APPLIED
history and run log still reference it. Deleting would orphan that history, or,
with a cascade, destroy it. Re-adding the company re-enables the same row.

**TOML, not YAML.** Python 3.11+ reads TOML with the standard library
(`tomllib`), so this needs no new dependency, and TOML has fewer surprises than
YAML (in YAML, a board named `no` becomes `false`).

**The registry name is the company name.** Greenhouse might call a company
"Stripe, Inc." while Lever calls it "Stripe". Using your registry name
everywhere gives the same company the same dedup key on every source, which
makes the level-3 fingerprint work *across* platforms.

**Constraints split between the database and the code.**
- In the database: one row per `(collector, board)`, priority between 1 and 3,
  and board slugs stored lowercase. These hold no matter who writes the row.
- In the code: whether a collector name is valid. The set of collectors grows
  with the code, and adding one shouldn't need a schema migration.

**`--priority N` means "N or more important".** `--priority 2` includes
priority 1. This mirrors how the scheduler will think: important companies
are a superset of the polling at every level.

## What this slice deliberately does *not* do

- **No polling intervals or state yet** (`next_poll_at`, failure counts). Those
  belong to the scheduler slice. Adding columns to an existing table is also
  where versioned migrations (Alembic) earn their place, so they arrive together.
- **No automatic company discovery.** Finding *new* companies to watch (from
  curated lists, or by detecting a careers page's platform) is a later slice.
  Today you add a line to `companies.toml`.
- **No foreign key from jobs to companies.** Jobs already carry
  `(source, board)`, which identifies the registry row. A real foreign key
  would mean altering the jobs table, so it waits for migrations too.

## Upgrading an existing database

`companies` is a new table, so `jobradar init-db` creates it without touching
your existing jobs (`create_all` only creates missing tables). This is the last
schema change that works that way. The next one alters a table.
