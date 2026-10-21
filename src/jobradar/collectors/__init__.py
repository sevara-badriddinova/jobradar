import httpx

from jobradar.collectors.ashby import AshbyCollector
from jobradar.collectors.base import CollectorError, JobCollector
from jobradar.collectors.greenhouse import GreenhouseCollector
from jobradar.collectors.lever import LeverCollector
from jobradar.collectors.workday import WorkdayCollector
from jobradar.collectors.workable import WorkableCollector
from jobradar.collectors.recruitee import RecruiteeCollector
from jobradar.collectors.rippling import RipplingCollector
from jobradar.collectors.personio import PersonioCollector
from jobradar.collectors.amazon import AmazonCollector
from jobradar.collectors.eightfold import EightfoldCollector
from jobradar.collectors.oracle_hcm import OracleHcmCollector

# collector name (as written in companies.toml) -> implementation.
# Adding a source = writing a class and adding it here.
COLLECTORS: dict[str, type[JobCollector]] = {
    cls.source: cls for cls in (GreenhouseCollector, LeverCollector, AshbyCollector,
                               WorkdayCollector, WorkableCollector, RecruiteeCollector,
                               RipplingCollector, PersonioCollector, AmazonCollector,
                               EightfoldCollector, OracleHcmCollector)
}


def build_collector(collector: str, board: str, company: str | None = None,
                    client: httpx.Client | None = None) -> JobCollector:
    if collector not in COLLECTORS:
        raise ValueError(f"unknown collector {collector!r}; known: {sorted(COLLECTORS)}")
    return COLLECTORS[collector](board, company=company, client=client)


__all__ = ["COLLECTORS", "AshbyCollector", "CollectorError", "JobCollector",
           "GreenhouseCollector", "LeverCollector", "WorkdayCollector", "WorkableCollector",
           "RecruiteeCollector", "RipplingCollector", "PersonioCollector", "AmazonCollector",
           "EightfoldCollector", "OracleHcmCollector", "build_collector"]
