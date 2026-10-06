# Slice 5 (plan step 6): hard filters and migrations

**Goal:** throw out the obvious no's (Senior roles, 5+ years required,
non-US jobs, non-engineering titles) with plain rules, before any AI is used.

```
collect ──► jobs (DISCOVERED) ──► jobradar filter ──► FILTERED (+ reason)
                                                 └──► DISCOVERED + filtered_at  → ready for the AI matcher
```

## The rules (src/jobradar/filters.py)

| Rule | Rejects when | Example reason |
|---|---|---|
| Title must include | none of `title_must_include` is in the title | `title: not a software engineering role` |
| Title must exclude | any of `title_must_exclude` is in the title | `title: senior` |
| Experience | the *smallest* required years stated is above `max_years_required` | `experience: requires 5+ years` |
| Location | `us_only` and **every** known location is outside the US | `location: London, United Kingdom` |

All of these are configurable in `profile.toml` under `[filters]`.

## Decisions and why

**Reject only when confident.** A wrongly rejected job is invisible: you never
see it, so you never know it was missed. A wrongly passed job just costs one
cheap AI call later. So every rule leans towards passing:
- **Location:** "Remote" with no country passes. A job in London *or* New York
  passes. Only jobs where every location is clearly non-US are rejected.
- **Experience:** the **smallest** number wins. "3+ years required, 5+ preferred"
  counts as 3, and sentences containing "preferred", "bonus", "nice to have" or
  "ideally" are ignored.
- **Experience text:** a number only counts if "experience" appears later in
  the same sentence, so "we've been around for 10 years" is ignored.

**Whole words, not substrings.** "lead" rejects "Tech Lead" but not "Leading
Edge Developer". "in" never means Indiana: US state codes only count in the
`City, ST` form. One known false friend is handled: "Member of Technical
Staff" (a common new-grad title at AI labs) is not a Staff-level role.

**Each collector says where its jobs are.** Platforms store extra locations
differently: Lever has `allLocations` + an ISO country code, Ashby has
`secondaryLocations` + address countries, Greenhouse has `offices`. Each
collector has a small static `locations(raw)` method, so the filter never needs
to know any platform's format. Because it reads the stored raw payload, it also
works on jobs collected before this slice existed.

**A filter decision is stored, not recomputed.** Each job gets `filtered_at`
(and `filter_reason` if rejected), so `filter` only looks at jobs it hasn't
seen. Rejected jobs get status `FILTERED`; passing jobs stay `DISCOVERED` with
`filtered_at` set, which is exactly what the AI matcher will pick up.

**`--recheck` after changing your settings.** It re-evaluates `DISCOVERED` and
`FILTERED` jobs, so loosening a rule brings jobs back. It never touches jobs
further along (matched, notified, applied).

**Duplicates, reposts and closed jobs aren't filtered.** They're never shown
to you anyway, so there's nothing to decide.

## Migrations (Alembic)

This slice is the first to add columns to an existing table, so `create_all`
(which only creates *missing* tables) is no longer enough.

- `src/jobradar/migrations/versions/` holds numbered migrations: `0001` is the
  schema from slices 1–4, and `0002` adds `filtered_at` and `filter_reason`.
- `jobradar init-db` now runs every migration that hasn't been applied yet. It's
  safe to run any time.
- A database created by an older `init-db` has the tables but no migration
  history. It's detected, recorded as being at `0001`, then upgraded, so your
  existing jobs are kept.
- Tests build their database through the migrations, and one test checks that
  the migrated schema is identical to what the code declares in `db.py`.
- To make a new migration after changing `db.py`:
  `uv run alembic revision --autogenerate -m "what changed"`.
