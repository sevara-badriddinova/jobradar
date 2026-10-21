"""Small public API shapes verified against vendor responses on 2026-10-13."""
import json

import httpx

from jobradar.collectors.eightfold import encode_board as eightfold_board
from jobradar.collectors.oracle_hcm import encode_board as oracle_board
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
        if source == "amazon":
            return httpx.Response(200, json=amazon_page([amazon_item("1"), amazon_item("2")], hits=2))
        if source == "oracle":
            if path.endswith("Details"):
                return httpx.Response(200, json=oracle_detail(request.url.params["finder"].split('"')[1]))
            return httpx.Response(200, json=oracle_page([oracle_item("1"), oracle_item("2")], total=2))
        if source == "eightfold":
            if path.endswith("position_details"):
                return httpx.Response(200, json=eightfold_detail(int(request.url.params["position_id"])))
            return httpx.Response(200, json={"data": {"count": 2, "positions": [eightfold_item(1), eightfold_item(2)]}})
        raise AssertionError(source)
    return response


# Big-company career APIs (shapes verified 2026-10-21).
DAY = "October 12, 2026"
ORACLE_BOARD = oracle_board("acme.fa.us2", "CX_1001", "300000000149325")
EIGHTFOLD_BOARD = eightfold_board("careers.acme.com", "acme.com", "pcsx", {"location": "United States"})
EIGHTFOLD_V2_BOARD = eightfold_board("careers.acme.com", "acme.com", "v2")
TS = 1792152000  # 2026-10-16T12:00:00Z


def amazon_item(i="1", posted=DAY, title=None):
    return {"id": "uuid-" + i, "id_icims": i, "title": title or "Software Engineer " + i, "posted_date": posted,
            "job_path": f"/en/jobs/{i}/software-engineer", "location": "US, NY, New York",
            "normalized_location": "New York, New York, USA", "country_code": "USA", "city": "New York",
            "locations": [json.dumps({"normalizedStateName": "New York", "normalizedCountryCode": "USA",
                                      "city": "New York"})],
            "description": DESCRIPTION, "basic_qualifications": "- 3+ years of non-internship experience",
            "preferred_qualifications": "- Bachelor's degree", "updated_time": "about 15 hours",
            "team": {"label": "team-sde"}, "display_distance": None}


def amazon_page(jobs, hits):
    return {"error": None, "hits": hits, "facets": {}, "content": {}, "jobs": jobs}


def oracle_item(i="1", posted="2026-10-12", title=None, end=None):
    return {"Id": i, "Title": title or "Software Engineer " + i, "PostedDate": posted, "PostingEndDate": end,
            "PrimaryLocation": "New York, NY, United States", "PrimaryLocationCountry": "US",
            "Relevancy": 1.0, "Distance": 0, "ShortDescriptionStr": "Build services",
            "secondaryLocations": [{"Name": "Austin, TX, United States", "CountryCode": "US"}]}


def oracle_page(reqs, total, offset=0):
    return {"items": [{"TotalJobsCount": total, "Offset": offset, "Limit": 200, "requisitionList": reqs}],
            "count": 1, "hasMore": False}


def oracle_detail(i="1", end=None):
    return {"items": [{"Id": i, "Title": "Software Engineer " + i, "ExternalPostedStartDate": "2026-10-12T12:00:00+00:00",
                       "ExternalPostedEndDate": end, "ExternalDescriptionStr": DESCRIPTION,
                       "ExternalQualificationsStr": "<p>3 years of Java</p>", "PrimaryLocation": "New York, NY, United States",
                       "PrimaryLocationCountry": "US",
                       "workLocation": [{"TownOrCity": "New York", "Region2": "NY", "Country": "US"}]}]}


def eightfold_item(i=1, posted=TS, created=TS - 86400, title=None):
    return {"id": i, "name": title or f"Software Engineer {i}", "locations": ["United States, New York, New York"],
            "standardizedLocations": ["New York, NY, US"], "postedTs": posted, "creationTs": created,
            "department": "Software Engineering", "solrScore": 3.2, "stars": 0.1, "atsJobId": str(i),
            "positionUrl": f"/careers/job/{i}"}


def eightfold_detail(i=1, posted=TS, created=TS - 86400):
    return {"data": {"id": i, "name": f"Software Engineer {i}", "jobDescription": DESCRIPTION, "postedTs": posted,
                     "creationTs": created, "locations": ["United States, New York, New York"],
                     "publicUrl": f"https://careers.acme.com/careers/job/{i}"}}
