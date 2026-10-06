import pytest
from pydantic import ValidationError

from jobradar.models import Job
from tests.factories import make_job


def test_valid_job_is_built_and_trimmed():
    job = Job(source="greenhouse", source_job_id=" 42 ", company=" Acme ", title=" SWE ",
              location="  ", url=" https://x.com/j/42 ", description="", posted_at=None)
    assert (job.source_job_id, job.company, job.title, job.url) == ("42", "Acme", "SWE", "https://x.com/j/42")
    assert job.location is None  # blank location normalized to None
    assert job.raw == {}


@pytest.mark.parametrize("field", ["source_job_id", "company", "title", "url"])
def test_required_identity_fields_cannot_be_blank(field):
    kwargs = dict(source="greenhouse", source_job_id="1", company="Acme", title="SWE",
                  location=None, url="https://x.com", description="", posted_at=None)
    kwargs[field] = "   "
    with pytest.raises(ValidationError):
        Job(**kwargs)


def test_job_is_immutable():
    job = make_job()
    with pytest.raises(ValidationError):
        job.title = "changed"


def test_raw_payload_is_hidden_from_repr():
    assert "raw" not in repr(make_job())
