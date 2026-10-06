import json
from datetime import UTC, datetime

import httpx
import pytest

from jobradar.notion import Notion, NotionError, notion_id, parse_page
from jobradar.settings import NotionSettings
from tests.factories import RecordingTransport

SOURCE = "11111111-1111-1111-1111-111111111111"
SETTINGS = NotionSettings(date_property="Applied on")


def page(i=1, status="Applied", company="Acme", role="Software Engineer", edited="2026-10-01T10:00:00Z"):
    return {"id": str(i), "last_edited_time": edited, "properties": {
        "Company": {"type": "title", "title": [{"plain_text": company}]},
        "Role": {"type": "rich_text", "rich_text": [{"plain_text": role}]},
        "Status": {"type": "status", "status": {"name": status}},
        "Applied on": {"type": "date", "date": {"start": "2026-09-30"}},
    }}


def schema():
    return {"properties": {key: {"type": value["type"]} for key, value in page()["properties"].items()}}


def test_maps_structured_fields_without_ai():
    assert parse_page(page(), SETTINGS) == {
        "company": "Acme", "role": "Software Engineer", "kind": "APPLIED",
        "occurred_at": datetime(2026, 9, 30, tzinfo=UTC),
    }


@pytest.mark.parametrize("status,kind", [("Rejected", "REJECTED"), ("interview", "INTERVIEW"), ("Offer", "OFFER")])
def test_status_mapping(status, kind):
    assert parse_page(page(status=status), SETTINGS)["kind"] == kind


@pytest.mark.parametrize("status", ["To apply", "Saved", ""])
def test_unsubmitted_rows_are_not_applications(status):
    assert parse_page(page(status=status), SETTINGS) is None


def test_custom_mapping_and_columns():
    p = page(status="Phone screen")
    p["properties"]["Position"] = p["properties"].pop("Role")
    settings = NotionSettings(role_property="Position", status_map={"Phone screen": "INTERVIEW"})
    assert parse_page(p, settings)["kind"] == "INTERVIEW"


def test_missing_role_does_not_guess():
    assert parse_page(page(role=""), SETTINGS)["role"] is None


@pytest.mark.parametrize("p", [page(status="Unexpected"), page(company=""), {"properties": {}},
                              page() | {"properties": {"Status": {"type": "relation"}}}])
def test_malformed_or_unknown_rows_fail_explicitly(p):
    with pytest.raises(NotionError):
        parse_page(p, SETTINGS)


def test_bad_date_and_archived_rows():
    p = page()
    p["properties"]["Applied on"]["date"]["start"] = "bad"
    with pytest.raises(NotionError, match="date"):
        parse_page(p, SETTINGS)
    assert parse_page(p | {"archived": True}, SETTINGS) is None


def test_paginates_all_500_rows_and_sends_version_and_auth():
    def respond(request):
        body = json.loads(request.content)
        offset = int(body.get("start_cursor", 0))
        assert body["page_size"] == 100
        assert request.headers["Notion-Version"] == "2025-09-03"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"results": [page(i) for i in range(offset, offset + 100)],
                                        "has_more": offset < 400, "next_cursor": str(offset + 100)})
    transport = RecordingTransport(respond)
    client = Notion("test-token", transport.client(), sleep=lambda _: None)
    assert len(list(client.pages(SOURCE, None))) == 500
    assert len(transport.requests) == 5


def test_incremental_filter():
    transport = RecordingTransport(httpx.Response(200, json={"results": [], "has_more": False}))
    client = Notion("x", transport.client(), sleep=lambda _: None)
    when = datetime(2026, 10, 1, tzinfo=UTC)
    assert list(client.pages(SOURCE, when)) == []
    assert json.loads(transport.requests[0].content)["filter"] == {
        "timestamp": "last_edited_time", "last_edited_time": {"on_or_after": when.isoformat()}}


@pytest.mark.parametrize("payload", [
    {"results": [], "has_more": True, "next_cursor": None},
    {"results": [], "has_more": True, "next_cursor": "same"},
    {"results": []},
    {"results": [], "has_more": False, "request_status": {"type": "incomplete"}},
])
def test_bad_pagination_never_looks_complete(payload):
    transport = RecordingTransport(httpx.Response(200, json=payload))
    with pytest.raises(NotionError):
        list(Notion("x", transport.client(), sleep=lambda _: None).pages(SOURCE, None))


def test_rate_limit_wait_and_transient_retry():
    transport = RecordingTransport(httpx.Response(429, headers={"Retry-After": "45"}),
                                   httpx.Response(503), httpx.Response(200, json={}))
    sleeps = []
    assert Notion("x", transport.client(), sleep=sleeps.append).request("GET", "/test") == {}
    assert 45 in sleeps and 2 in sleeps


def test_long_rate_limit_defers_without_retrying_early():
    transport = RecordingTransport(httpx.Response(429, headers={"Retry-After": "1000"}))
    with pytest.raises(NotionError, match="retry sync later"):
        Notion("x", transport.client(), sleep=lambda _: None).request("GET", "/test")
    assert len(transport.requests) == 1


@pytest.mark.parametrize("response", [httpx.Response(401, text="secret-token"), httpx.Response(200, text="bad")])
def test_errors_do_not_expose_token_or_body(response):
    transport = RecordingTransport(response)
    with pytest.raises(NotionError) as error:
        Notion("secret-token", transport.client()).request("GET", "/test")
    assert "secret-token" not in str(error.value)


def test_schema_validation_and_database_resolution():
    transport = RecordingTransport(httpx.Response(200, json={"data_sources": [{"id": SOURCE}]}),
                                   httpx.Response(200, json=schema()))
    client = Notion("x", transport.client(), sleep=lambda _: None)
    assert client.resolve_source(database_id=SOURCE) == SOURCE
    client.validate_schema(SOURCE, SETTINGS)
    with pytest.raises(NotionError, match="missing or unsupported"):
        client.validate_schema(SOURCE, NotionSettings(role_property="Wrong"))


def test_ambiguous_database_requires_explicit_source():
    transport = RecordingTransport(httpx.Response(200, json={"data_sources": [{"id": SOURCE}] * 2}))
    with pytest.raises(NotionError, match="explicitly"):
        Notion("x", transport.client()).resolve_source(database_id=SOURCE)


def test_id_from_url_ignores_view_id():
    assert notion_id(f"https://www.notion.so/Tracker-{SOURCE.replace('-', '')}?v=another-view") == SOURCE
    with pytest.raises(NotionError):
        notion_id("not-a-uuid")
