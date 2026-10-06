"""Read a structured Notion application tracker without making any AI calls."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from jobradar.applications import record
from jobradar.db import app_state
from jobradar.settings import NotionSettings

API_URL = "https://api.notion.com/v1"
API_VERSION = "2025-09-03"
TEXT_TYPES = {"title", "rich_text", "select", "status", "formula"}


class NotionError(Exception):
    """Notion import failed; no partial import/checkpoint was committed."""


def notion_id(value: str) -> str:
    """Accept a UUID or a copied Notion database URL (excluding view query IDs)."""
    value = value.strip().split("?", 1)[0].rstrip("/").split("/")[-1]
    match = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$", value)
    if not match:
        raise NotionError("expected a Notion database/data-source UUID or database URL")
    return str(UUID(match.group(1)))


class Notion:
    def __init__(self, token: str, client: httpx.Client, *, sleep=time.sleep):
        self.client, self.sleep = client, sleep
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": API_VERSION}
        self.last_request = None

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(3):
            if self.last_request is not None:
                self.sleep(max(0, 0.35 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.client.request(method, API_URL + path, headers=self.headers,
                                               json=body, timeout=30, follow_redirects=False)
            except httpx.TransportError:
                if attempt == 2:
                    raise NotionError("Notion connection failed after 3 attempts") from None
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                try:
                    delay = float(response.headers.get("Retry-After", 2 ** attempt))
                except ValueError:
                    delay = 2 ** attempt
                if attempt == 2 or not 0 <= delay <= 120:
                    raise NotionError(f"Notion HTTP {response.status_code}; retry sync later")
                self.sleep(delay)
                continue
            if response.status_code != 200:
                # Do not log request headers or reflect arbitrary response text.
                raise NotionError(f"Notion HTTP {response.status_code}; check token, tracker ID, and connection access")
            try:
                data = response.json()
            except ValueError:
                raise NotionError("Notion returned invalid JSON") from None
            if not isinstance(data, dict):
                raise NotionError("Notion returned an invalid response")
            return data
        raise NotionError("Notion retries exhausted")

    def resolve_source(self, *, data_source_id: str = "", database_id: str = "") -> str:
        if data_source_id:
            return notion_id(data_source_id)
        if not database_id:
            raise NotionError("set NOTION_DATA_SOURCE_ID or NOTION_DATABASE_ID")
        data = self.request("GET", f"/databases/{notion_id(database_id)}")
        sources = data.get("data_sources", [])
        if len(sources) != 1:
            raise NotionError("database must have exactly one data source; set NOTION_DATA_SOURCE_ID explicitly")
        return notion_id(sources[0].get("id", ""))

    def validate_schema(self, source_id: str, settings: NotionSettings) -> None:
        properties = self.request("GET", f"/data_sources/{source_id}").get("properties", {})
        expected = {settings.company_property: TEXT_TYPES, settings.role_property: TEXT_TYPES,
                    settings.status_property: TEXT_TYPES}
        if settings.date_property:
            expected[settings.date_property] = {"date"}
        for name, types in expected.items():
            if properties.get(name, {}).get("type") not in types:
                raise NotionError(f"Notion property {name!r} is missing or unsupported; check [notion] column mapping")

    def pages(self, source_id: str, since: datetime | None):
        body = {"page_size": 100, "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}]}
        if since:
            body["filter"] = {"timestamp": "last_edited_time",
                              "last_edited_time": {"on_or_after": since.isoformat()}}
        cursors = set()
        for _ in range(1000):
            data = self.request("POST", f"/data_sources/{source_id}/query", body)
            if (not isinstance(data.get("results"), list) or
                    not isinstance(data.get("has_more"), bool)):
                raise NotionError("Notion returned invalid pagination metadata")
            if (data.get("request_status") or {}).get("type", "complete") != "complete":
                raise NotionError("Notion query was incomplete; checkpoint unchanged")
            yield from data["results"]
            if not data["has_more"]:
                return
            cursor = data.get("next_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise NotionError("Notion pagination cursor is missing or repeated")
            cursors.add(cursor)
            body["start_cursor"] = cursor
        raise NotionError("Notion pagination limit exceeded; checkpoint unchanged")


def _text(prop: dict) -> str:
    kind = prop.get("type")
    if kind in {"title", "rich_text"}:
        return "".join(part.get("plain_text", part.get("text", {}).get("content", ""))
                       for part in prop.get(kind, [])).strip()
    if kind in {"select", "status"}:
        return (prop.get(kind) or {}).get("name", "").strip()
    if kind == "formula" and prop.get("formula", {}).get("type") == "string":
        return (prop["formula"].get("string") or "").strip()
    raise NotionError(f"unsupported tracker value type: {kind!r}")


def _date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, AttributeError):
        raise NotionError("Notion returned an invalid date") from None


def parse_page(page: dict, settings: NotionSettings) -> dict | None:
    if page.get("archived") or page.get("in_trash") or page.get("is_archived"):
        return None  # deleting a tracker row never erases application history
    props = page.get("properties", {})
    try:
        status = _text(props[settings.status_property])
        mapping = {name.strip().casefold(): kind for name, kind in settings.status_map.items()}
        if not status:
            return None
        kind = mapping.get(status.casefold())
        if kind is None:
            raise NotionError(f"unmapped Notion status {status!r}; add it to [notion.status_map]")
        if kind == "SKIP":
            return None
        company = _text(props[settings.company_property])
        role = _text(props[settings.role_property])
        if not company:
            raise NotionError("an application row has no company")
        date_value = (props[settings.date_property].get("date") or {}).get("start") if settings.date_property else None
        return dict(company=company, role=role or None, kind=kind,
                    occurred_at=_date(date_value) if date_value else None)
    except (KeyError, TypeError, AttributeError):
        raise NotionError("invalid Notion application properties; check column mapping") from None


@dataclass
class NotionResult:
    read: int = 0
    changed: int = 0
    skipped: int = 0


def sync_notion(engine: Engine, client: Notion, settings: NotionSettings, *,
                data_source_id: str = "", database_id: str = "", full: bool = False) -> NotionResult:
    """Import a complete incremental batch atomically. Source IDs make retries safe."""
    source_id = client.resolve_source(data_source_id=data_source_id, database_id=database_id)
    mapping_hash = hashlib.sha256(json.dumps(settings.model_dump(), sort_keys=True).encode()).hexdigest()
    key = f"notion:{source_id}:{mapping_hash}"
    result = NotionResult()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                     {"key": f"jobradar:notion:{source_id}"})
        saved = conn.execute(select(app_state.c.value).where(app_state.c.key == key)).scalar_one_or_none()
        checkpoint = _date(saved) if saved else None
        # Inclusive overlap handles timestamp ties/eventual visibility; upserts
        # are no-ops for unchanged rows. Changed mappings automatically backfill.
        since = checkpoint - timedelta(minutes=1) if checkpoint and not full else None
        client.validate_schema(source_id, settings)
        latest = checkpoint
        for page in client.pages(source_id, since):
            if not isinstance(page, dict) or not isinstance(page.get("id"), str) or not page["id"]:
                raise NotionError("Notion returned a row without a stable page ID")
            edited = _date(page.get("last_edited_time"))
            latest = max(latest, edited) if latest else edited
            result.read += 1
            values = parse_page(page, settings)
            if values is None:
                result.skipped += 1
                continue
            result.changed += record(conn, source="notion", external_id=page["id"], **values)
        if latest and latest != checkpoint:
            conn.execute(insert(app_state).values(key=key, value=latest.isoformat())
                         .on_conflict_do_update(index_elements=["key"], set_={"value": latest.isoformat()}))
    return result
