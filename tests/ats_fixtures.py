"""Small public API shapes verified against vendor responses on 2026-10-13."""
import json

import httpx

from jobradar.collectors.workday import encode_board

WORKDAY_BOARD = encode_board("wd1", "acme", "External_Careers")
DATE = "2026-10-12T12:00:00Z"
DESCRIPTION = "<p>Build backend services. We offer H-1B visa sponsorship for this role.</p>"


def workday_item(i="1", posted="Posted Yesterday"):
    return {"title": "Software Engineer " + i, "externalPath": "/job/USA/Engineer_R" + i,
            "locationsText": "New York, NY", "postedOn": posted, "bulletFields": ["R" + i]}


def workday_detail(i="1"):
    return {"jobPostingInfo": {"id": i, "title": "Software Engineer " + i,
        "jobDescription": DESCRIPTION, "startDate": DATE, "location": "New York, NY",
        "country": {"descriptor": "United States of America"}, "canApply": True, "posted": True}}


def workable_item(i="1"):
    return {"shortcode": i, "title": "Software Engineer " + i, "url": "https://apply.workable.com/j/" + i,
            "description": DESCRIPTION, "published_on": DATE, "city": "New York", "country": "United States",
            "locations": [{"countryCode": "US", "city": "New York"}]}


def recruitee_item(i="1"):
    return {"id": i, "title": "Software Engineer " + i, "careers_url": "https://acme.recruitee.com/o/" + i,
            "description": DESCRIPTION, "requirements": "<p>Python</p>", "status": "published",
            "published_at": "2026-10-12 12:00:00 UTC", "location": "New York", "country_code": "US"}


def rippling_item(i="1"):
    return {"uuid": i, "name": "Software Engineer " + i, "description": {"role": DESCRIPTION},
            "workLocations": ["New York, NY"], "createdOn": DATE,
            "jsonLd": {"@type": "JobPosting", "datePosted": DATE}}


def personio_xml(ids=("1", "2")):
    return '<workzag-jobs>' + ''.join(f'''<position><id>{i}</id><name>Software Engineer {i}</name>
    <office>New York</office><createdAt>{DATE}</createdAt><jobDescriptions><jobDescription>
    <name>The role</name><value><![CDATA[{DESCRIPTION}]]></value></jobDescription></jobDescriptions>
    <additionalOffices><office>Boston, MA</office><office>Remote US</office></additionalOffices>
    </position>''' for i in ids) + '</workzag-jobs>'


def linked_data(date=DATE):
    return '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "datePosted": date,
        "jobLocation": {"address": {"addressCountry": "US"}}}) + '</script>'


def public_handler(source):
    def response(request):
        path = request.url.path
        if source == "workday":
            if request.method == "POST":
                return httpx.Response(200, json={"total": 2, "jobPostings": [workday_item("1"), workday_item("2")]})
            return httpx.Response(200, json=workday_detail(path[-1]))
        if source == "workable":
            return httpx.Response(200, json={"jobs": [workable_item("1"), workable_item("2")]})
        if source == "recruitee":
            return httpx.Response(200, json={"offers": [recruitee_item("1"), recruitee_item("2")]})
        if source == "rippling":
            if path.endswith("/jobs"):
                return httpx.Response(200, json=[{"uuid": "1"}, {"uuid": "2"}])
            return httpx.Response(200, json=rippling_item(path[-1]))
        if source == "personio":
            return httpx.Response(200, text=personio_xml() if path == "/xml" else linked_data())
        raise AssertionError(source)
    return response
