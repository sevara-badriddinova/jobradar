from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from jobradar.notifications import _ago, digest_messages, job_buttons, job_message
from jobradar.settings import NotificationSettings
from jobradar.telegram import MAX_MESSAGE_CHARS

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def job(**kw):
    return SimpleNamespace(**({
        "id": 7, "company": "Stripe", "title": "Software Engineer, New Grad", "location": "New York, NY",
        "url": "https://stripe.com/jobs?gh_jid=1", "first_seen_at": NOW - timedelta(minutes=3),
        "closed_at": None, "match_score": 92.4,
        "assessment": {"reasons": ["New-grad eligible", "Java/Spring match"], "concerns": [],
                       "missing_skills": ["Kafka"]}} | kw))


def test_message_layout():
    text = job_message(job(), NOW)
    assert text.splitlines()[:3] == ["🎯 <b>92% match · Software Engineer, New Grad</b>",
                                     "Stripe · New York, NY", "<i>found 3 min ago</i>"]
    assert "<b>Why</b>\n• New-grad eligible\n• Java/Spring match" in text
    assert "Concerns" not in text and "<b>Missing:</b> Kafka" in text


def test_untrusted_text_is_html_escaped():
    text = job_message(job(title="<script>x</script> & Co", company="A&B",
                           assessment={"reasons": ["uses <b> tags"], "concerns": [], "missing_skills": []}), NOW)
    assert "<script>" not in text and "&lt;script&gt;" in text and "A&amp;B" in text
    assert "uses &lt;b&gt; tags" in text


def test_long_fields_are_clipped():
    text = job_message(job(title="T" * 500, assessment={"reasons": ["r" * 900] * 9, "concerns": [],
                                                       "missing_skills": []}), NOW)
    assert len(text) < 2000 and text.count("•") == 4


def test_missing_assessment_still_formats():
    assert "Why" not in job_message(job(assessment=None, location=None), NOW)


@pytest.mark.parametrize("delta,expected", [(timedelta(seconds=20), "just now"), (timedelta(minutes=59), "59 min ago"),
                                            (timedelta(hours=5), "5 h ago"), (timedelta(days=3), "3 d ago")])
def test_ago(delta, expected):
    assert _ago(NOW - delta, NOW) == expected


def test_buttons():
    buttons = job_buttons(job())
    assert buttons[0] == [{"text": "Apply ↗", "url": "https://stripe.com/jobs?gh_jid=1"}]
    actions = {button["callback_data"] for row in buttons for button in row if "callback_data" in button}
    assert actions == {"applied:7", "ignored:7", "queue:7", "sponsorship:7", "show_queue"}
    assert job_buttons(job(url="javascript:alert(1)"))[0][0]["text"] == "✅ Applied"
    assert any(b["callback_data"] == "ignored:7" for row in job_buttons(job(status="APPLIED")) for b in row if "callback_data" in b)


def test_callback_data_fits_telegrams_64_byte_limit():
    assert all(len(b["callback_data"].encode()) <= 64 for row in job_buttons(job(id=2**63-1))
               for b in row if "callback_data" in b)


def test_digest_lists_jobs_with_links_and_splits_long_digests():
    items = [(i, job(id=i, title=f"Role {i}", match_score=75)) for i in range(200)]
    messages = digest_messages(items)
    assert len(messages) > 1 and sum(len(chunk) for _, chunk in messages) == 200
    assert all(len(text) <= MAX_MESSAGE_CHARS for text, _ in messages)
    first_text, first_chunk = messages[0]
    assert first_text.startswith("📬 <b>Digest") and '<a href="https://stripe.com/jobs?gh_jid=1">Role 0</a>' in first_text
    assert first_chunk[0][0] == 0


def test_empty_digest_sends_nothing():
    assert digest_messages([]) == []


def test_threshold_order_is_validated():
    with pytest.raises(ValueError, match="digest_min_score"):
        NotificationSettings(immediate_min_score=60, digest_min_score=70)
