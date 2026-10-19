import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobradar.collectors import COLLECTORS, CollectorError
from jobradar.collectors.public import posting_metadata
from jobradar.collectors.workday import decode_board, encode_board
from jobradar.discovery import directory_board
from jobradar.filters import sponsorship_evidence, sponsorship_reason
from jobradar.recency import publication_reason
from tests.ats_fixtures import (DATE, WORKDAY_BOARD, linked_data, personio_xml, public_handler,
    recruitee_item, rippling_item, workable_item, workday_detail, workday_item)
from tests.factories import RecordingTransport

NEW = ['workday', 'workable', 'recruitee', 'rippling', 'personio']


def collector(source, handler=None, board=None):
    transport = RecordingTransport(handler or public_handler(source))
    return COLLECTORS[source](board or (WORKDAY_BOARD if source == 'workday' else 'acme'),
                              company='Acme', client=transport.client()), transport


@pytest.mark.parametrize('source', NEW)
def test_publication_description_and_locations_reach_policy(source):
    c, _ = collector(source)
    j = c.collect()[0]
    assert j.posted_at == datetime(2026, 10, 12, 12, tzinfo=UTC)
    assert sponsorship_evidence(j.description)
    assert sponsorship_reason(j.description) is None
    assert c.locations(j.raw)
    assert c.complete_snapshot


@pytest.mark.parametrize('source', NEW)
def test_old_jobs_cannot_pass_three_day_policy(source):
    c, _ = collector(source)
    j = c.collect()[0]
    assert publication_reason(j.posted_at, 3, now=j.posted_at + timedelta(days=3, seconds=1))
    assert publication_reason(j.posted_at, 3, now=j.posted_at + timedelta(days=2)) is None


def test_workday_preserves_site_case_and_namespaces_requisition_ids():
    assert decode_board(WORKDAY_BOARD) == ('wd1', 'acme', 'External_Careers')
    c, t = collector('workday')
    a = c.collect()[0]
    assert all('/External_Careers/' in r.url.path for r in t.requests)
    other, _ = collector('workday', board=encode_board('wd1', 'other', 'External_Careers'))
    assert a.source_job_id != other.collect()[0].source_job_id
    assert a.url.endswith('/External_Careers/job/USA/Engineer_R1')


def test_workday_paginates_with_real_offsets_and_post_body():
    def handler(request):
        if request.method == 'POST':
            body = json.loads(request.content)
            assert body['limit'] == 20 and body['searchText'] == '' and body['appliedFacets'] == {}
            offset = body['offset']
            return httpx.Response(200, json={'total': 3, 'jobPostings':
                [workday_item(str(i)) for i in ([1, 2] if offset == 0 else [3])]})
        return httpx.Response(200, json=workday_detail(request.url.path[-1]))
    c, t = collector('workday', handler)
    assert len(c.collect()) == 3
    assert [json.loads(r.content)['offset'] for r in t.requests if r.method == 'POST'] == [0, 2]


@pytest.mark.parametrize('page', [[], [workday_item('1')]])
def test_workday_truncated_or_repeated_page_keeps_valid_jobs_without_closing(page):
    t = RecordingTransport(httpx.Response(200, json={'total': 3, 'jobPostings': [workday_item('1', posted='Posted 30+ Days Ago')]}),
                           httpx.Response(200, json={'total': 3, 'jobPostings': page}))
    c = COLLECTORS['workday'](WORKDAY_BOARD, client=t.client())
    assert len(c.collect()) == 1
    assert not c.complete_snapshot


def test_workday_old_relative_label_only_excludes_never_fabricates_timestamp():
    c, t = collector('workday', lambda r: httpx.Response(200, json={
        'total': 1, 'jobPostings': [workday_item(posted='Posted 30+ Days Ago')]}))
    assert c.collect()[0].posted_at is None
    assert len(t.requests) == 1


@pytest.mark.parametrize('label', ['Posted Today', 'Posted 3 Days Ago', 'Hier', ''])
def test_workday_unknown_or_recent_label_requires_absolute_detail_date(label):
    def handler(r):
        if r.method == 'POST':
            return httpx.Response(200, json={'total': 1, 'jobPostings': [workday_item(posted=label)]})
        d = workday_detail(); d['jobPostingInfo'].pop('startDate')
        return httpx.Response(200, json=d)
    c, t = collector('workday', handler)
    assert c.collect()[0].posted_at is None and len(t.requests) == 2


@pytest.mark.parametrize('source', ['workday', 'rippling', 'personio'])
def test_failed_single_detail_marks_snapshot_incomplete(source):
    def handler(r):
        if r.method != 'POST' and r.url.path.endswith(('R2', '/2')):
            return httpx.Response(404)
        return public_handler(source)(r)
    c, _ = collector(source, handler)
    assert len(c.collect()) == 1
    assert not c.complete_snapshot


@pytest.mark.parametrize('source', ['workable', 'recruitee'])
def test_malformed_record_prevents_closure(source):
    key, factory = ('jobs', workable_item) if source == 'workable' else ('offers', recruitee_item)
    c, _ = collector(source, lambda r: httpx.Response(200, json={key: [factory(), {'id': 'broken'}]}))
    assert len(c.collect()) == 1 and not c.complete_snapshot


def test_workable_requests_details_and_does_not_use_creation_date():
    raw = workable_item();raw.pop('published_on'); raw['created_at'] = DATE
    c, t = collector('workable', lambda r: httpx.Response(200, json={'jobs': [raw]}))
    assert c.collect()[0].posted_at is None
    assert t.requests[0].url.params['details'] == 'true'


def test_recruitee_excludes_drafts_and_ignores_updated_at():
    raw = recruitee_item();raw.pop('published_at');raw['updated_at'] = DATE
    draft = recruitee_item('2');draft['status'] = 'draft'
    c, _ = collector('recruitee', lambda r: httpx.Response(200, json={'offers': [raw, draft]}))
    assert len(jobs := c.collect()) == 1 and jobs[0].posted_at is None
    assert 'Python' in jobs[0].description


@pytest.mark.parametrize('source', ['rippling', 'personio'])
def test_creation_and_discovery_dates_never_substitute_for_missing_publication(source):
    def handler(r):
        if source == 'personio':
            return httpx.Response(200, text=personio_xml(('1',)) if r.url.path == '/xml' else '<html>No date</html>')
        if r.url.path.endswith('/jobs'):
            return httpx.Response(200, json=[{'uuid': '1'}])
        if r.url.host == 'ats.rippling.com':
            return httpx.Response(200, text='<html>No date</html>')
        raw = rippling_item();raw['jsonLd'] = None
        return httpx.Response(200, json=raw)
    c, _ = collector(source, handler)
    assert c.collect()[0].posted_at is None


@pytest.mark.parametrize('xml', ['<html/>', '<workzag-jobs><garbage/></workzag-jobs>',
    '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><workzag-jobs/>'])
def test_personio_rejects_malformed_and_entity_xml(xml):
    c, _ = collector('personio', lambda r: httpx.Response(200, text=xml))
    with pytest.raises(CollectorError):
        c.collect()


@pytest.mark.parametrize('source,payload', [('workday', {'total': 0, 'jobPostings': []}),
    ('workable', {'jobs': []}), ('recruitee', {'offers': []}), ('rippling', [])])
def test_empty_boards_are_valid(source, payload):
    c, _ = collector(source, lambda r: httpx.Response(200, json=payload))
    assert c.collect() == []


def test_personio_empty_feed():
    c, _ = collector('personio', lambda r: httpx.Response(200, text='<workzag-jobs/>'))
    assert c.collect() == []


def test_linked_data_graph_requires_unambiguous_job():
    good = {'@type': 'JobPosting', 'datePosted': DATE}
    assert posting_metadata({'@graph': [{'@type': 'WebPage', 'dateModified': DATE}, good]}) == good
    assert posting_metadata([good, {**good, 'title': 'Other role'}]) == {}
    assert posting_metadata('<script type="application/ld+json">bad</script>') == {}


@pytest.mark.parametrize('platform,token,url,expected', [
    ('workday', 'wd1/acme/External_Careers', 'https://acme.wd1.myworkdayjobs.com/External_Careers', WORKDAY_BOARD),
    ('workable', 'acme', 'https://apply.workable.com/acme', 'acme'),
    ('recruitee', 'acme', 'https://acme.recruitee.com', 'acme'),
    ('rippling', 'acme', 'https://ats.rippling.com/acme', 'acme'),
    ('personio', 'acme', 'https://acme.jobs.personio.de', 'acme'),
    ('personio', 'acme', 'https://acme.jobs.personio.com', 'com:acme'),
])
def test_discovery_maps_vendor_addresses(platform, token, url, expected):
    assert directory_board(platform, token, url) == expected


@pytest.mark.parametrize('url', ['https://acme.recruitee.com.evil.test',
    'https://evil.test/acme', 'http://acme.recruitee.com', 'https://me@acme.recruitee.com',
    'https://acme.recruitee.com:999', 'https://acme.recruitee.com/?url=evil'])
def test_discovery_rejects_untrusted_addresses(url):
    with pytest.raises(ValueError):
        directory_board('recruitee', 'acme', url)


def test_workday_later_zero_totals_do_not_truncate_at_second_page():
    def handler(r):
        offset = json.loads(r.content)['offset']
        count = min(20, 45 - offset)
        return httpx.Response(200, json={'total': 45 if offset == 0 else 0, 'jobPostings': [
            workday_item(str(i), posted='Posted 30+ Days Ago') for i in range(offset, offset + count)]})
    c, t = collector('workday', handler)
    assert len(c.collect()) == 45
    assert [json.loads(r.content)['offset'] for r in t.requests] == [0, 20, 40]


@pytest.mark.parametrize('field', ['posted', 'canApply'])
def test_workday_closed_or_non_applicable_details_are_excluded(field):
    def handler(r):
        if r.method == 'POST':
            return httpx.Response(200, json={'total': 1, 'jobPostings': [workday_item()]})
        raw = workday_detail();raw['jobPostingInfo'][field] = False
        return httpx.Response(200, json=raw)
    c, _ = collector('workday', handler)
    assert c.collect() == []


@pytest.mark.parametrize('bad', [None, {'bulletFields': ['R2']}, {'externalPath': 'https://evil.test/job/2'},
    {'externalPath': '/job/../private'}, {'externalPath': '/job/../../private'}])
def test_workday_malformed_entry_does_not_discard_other_jobs_or_truncate_next_page(bad):
    def handler(r):
        if r.method != 'POST':
            return httpx.Response(200, json=workday_detail(r.url.path[-1]))
        offset = json.loads(r.content)['offset']
        return httpx.Response(200, json={'total': 3 if offset == 0 else 0,
            'jobPostings': [workday_item('1'), bad] if offset == 0 else [workday_item('3')]})
    c, t = collector('workday', handler)
    assert len(c.collect()) == 2 and not c.complete_snapshot
    assert [json.loads(r.content)['offset'] for r in t.requests if r.method == 'POST'] == [0, 2]
    assert {r.url.host for r in t.requests} == {'acme.wd1.myworkdayjobs.com'}


@pytest.mark.parametrize('later', [httpx.Response(404), httpx.Response(200, json={'oops': 1})])
def test_workday_later_failed_page_retains_earlier_jobs(later):
    def handler(r):
        if r.method != 'POST':
            return httpx.Response(200, json=workday_detail())
        if json.loads(r.content)['offset'] == 0:
            return httpx.Response(200, json={'total': 2, 'jobPostings': [workday_item()]})
        return later
    c, _ = collector('workday', handler)
    assert len(c.collect()) == 1 and not c.complete_snapshot


def test_workday_page_limit_returns_incomplete_data_and_all_bad_still_fails():
    c, _ = collector('workday', lambda r: httpx.Response(200, json={
        'total': 100, 'jobPostings': [workday_item(posted='Posted 30+ Days Ago')]}))
    c.max_pages = 1
    assert len(c.collect()) == 1 and not c.complete_snapshot
    c, _ = collector('workday', lambda r: httpx.Response(200, json={
        'total': 1, 'jobPostings': [{'bulletFields': ['R2']}]}))
    with pytest.raises(CollectorError, match='no usable'):
        c.collect()


@pytest.mark.parametrize('english', ['<html>No metadata</html>', linked_data('invalid'),
                                   '<script type="application/ld+json">{"@type":"JobPosting"}</script>'])
def test_personio_default_language_recovers_missing_or_invalid_date(english):
    def handler(r):
        if r.url.path == '/xml':
            return httpx.Response(200, text=personio_xml(('1',)))
        return httpx.Response(200, text=english if r.url.params.get('language') == 'en' else linked_data())
    c, t = collector('personio', handler)
    job = c.collect()[0]
    assert job.posted_at == datetime(2026, 10, 12, 12, tzinfo=UTC)
    assert len(t.requests) == 3
    assert sponsorship_evidence(job.description)  # English XML description retained


def test_personio_does_not_overwrite_valid_english_date_or_use_created_at():
    c, t = collector('personio')
    assert all(j.posted_at for j in c.collect())
    assert len(t.requests) == 3  # feed + two English pages; no default fallback


def test_personio_missing_english_page_falls_back():
    def handler(r):
        if r.url.path == '/xml':return httpx.Response(200, text=personio_xml(('1',)))
        if r.url.params.get('language') == 'en':return httpx.Response(404)
        return httpx.Response(200, text=linked_data())
    c, _ = collector('personio', handler)
    assert c.collect()[0].posted_at is not None


def test_rippling_metadata_without_date_still_checks_public_page():
    def handler(r):
        if r.url.path.endswith('/jobs'):return httpx.Response(200, json=[{'uuid': '1'}])
        if r.url.host == 'ats.rippling.com':return httpx.Response(200, text=linked_data())
        raw = rippling_item(); raw['jsonLd'] = {'@type': 'JobPosting', 'title': 'Engineer'}
        return httpx.Response(200, json=raw)
    c, _ = collector('rippling', handler)
    assert c.collect()[0].posted_at == datetime(2026, 10, 12, 12, tzinfo=UTC)
