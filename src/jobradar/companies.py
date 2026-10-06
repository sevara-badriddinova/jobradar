"""Company registry: which boards to poll, and how important each one is.

companies.toml (in git, edited by you) is the declarative source of truth.
`sync` makes the database match it. The database is what the collector (and
later the scheduler) reads, because it can also hold runtime state such as
"when was this last polled".
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Row

from jobradar.collectors import COLLECTORS
from jobradar.db import companies as companies_t
from jobradar.dedup import company_key


class CompanyFileError(Exception):
    """companies.toml is invalid. Nothing is written when this is raised."""


class CompanySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)  # typos like "prority" fail loudly

    name: str
    collector: str
    board: str
    priority: int = Field(default=2, ge=1, le=3)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v

    @field_validator("board")
    @classmethod
    def _board(cls, v: str) -> str:
        v = v.strip().lower()
        if not v or "/" in v or " " in v:
            raise ValueError("must be the bare board slug, e.g. 'stripe'")
        return v

    @field_validator("collector")
    @classmethod
    def _collector(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in COLLECTORS:
            raise ValueError(f"unknown collector {v!r}; known: {sorted(COLLECTORS)}")
        return v


def parse_companies(data: dict) -> list[CompanySpec]:
    entries = data.get("company", [])
    if not isinstance(entries, list):
        raise CompanyFileError("expected [[company]] entries")
    specs, errors, seen = [], [], {}
    for i, entry in enumerate(entries, start=1):
        try:
            spec = CompanySpec.model_validate(entry)
        except ValidationError as exc:
            label = entry.get("name", f"entry #{i}") if isinstance(entry, dict) else f"entry #{i}"
            errors.append(f"{label}: " + "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'entry'}: {e['msg']}" for e in exc.errors()))
            continue
        key = (spec.collector, spec.board)
        if key in seen:
            errors.append(f"{spec.name}: duplicate of {seen[key]} ({spec.collector}/{spec.board})")
            continue
        seen[key] = spec.name
        specs.append(spec)
    if errors:
        # Report every problem at once, and write nothing: a half-applied
        # registry is worse than none.
        raise CompanyFileError("invalid companies file:\n  " + "\n  ".join(errors))
    return specs


def load_company_file(path: str | Path) -> list[CompanySpec]:
    try:
        data = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raise CompanyFileError(f"file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise CompanyFileError(f"{path}: invalid TOML: {exc}") from None
    return parse_companies(data)


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.disabled)


def sync_companies(conn: Connection, specs: list[CompanySpec]) -> SyncResult:
    """Make the companies table match `specs`. Idempotent; run inside one transaction."""
    result = SyncResult()
    existing = {(r.collector, r.board): r for r in conn.execute(select(companies_t))}

    for spec in specs:
        current = existing.get((spec.collector, spec.board))
        values = dict(name=spec.name, priority=spec.priority, enabled=spec.enabled)
        if current is None:
            conn.execute(insert(companies_t).values(collector=spec.collector, board=spec.board, **values))
            result.added.append(spec.name)
        elif any(getattr(current, k) != v for k, v in values.items()):
            conn.execute(update(companies_t).where(companies_t.c.id == current.id)
                         .values(updated_at=func.now(), **values))
            result.updated.append(spec.name)
        else:
            result.unchanged += 1

    # Entries removed from the file are disabled, never deleted: their jobs,
    # APPLIED history and run log stay meaningful.
    in_file = {(s.collector, s.board) for s in specs}
    for key, row in existing.items():
        if key not in in_file and row.enabled:
            conn.execute(update(companies_t).where(companies_t.c.id == row.id)
                         .values(enabled=False, updated_at=func.now()))
            result.disabled.append(row.name)
    return result


def matches(row: Row, name: str) -> bool:
    """True if `name` refers to this company, by board slug or by company name."""
    name = name.strip()
    return row.board == name.lower() or company_key(row.name) == company_key(name)


def list_companies(conn: Connection, *, enabled_only: bool = True,
                   priority: int | None = None, names: list[str] | None = None) -> list[Row]:
    """Registry rows, optionally filtered. `names` match the board or the company name.

    priority=N keeps companies at priority N or more important (1 is highest).
    """
    query = select(companies_t).order_by(companies_t.c.priority, companies_t.c.name)
    if enabled_only:
        query = query.where(companies_t.c.enabled)
    if priority is not None:
        query = query.where(companies_t.c.priority <= priority)
    rows = conn.execute(query).all()
    if names:
        rows = [r for r in rows if any(matches(r, n) for n in names)]
    return rows
