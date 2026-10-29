"""The puller against a fake FHIR server: paging, dedupe across rows, isolation, 401, expiry, resume."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from patient_access.core.models import Job, Participant, Secrets
from patient_access.core.pipeline import EHR_TOKEN, FetchAborted
from patient_access.core.puller import fetch
from patient_access.core.source_settings import QuerySpec
from patient_access.testing.memory import InMemorySpool, selection, settings, source

BASE = "https://ehr.example/fhir"
TABLE = (
    QuerySpec("Demographics", "Patient", "Patient", "patient/Patient.read", single=True),
    QuerySpec("Labs", "Observation", "Observation?category=laboratory", "patient/Observation.read"),
    QuerySpec("Vitals", "Observation", "Observation?category=vital-signs", "patient/Observation.read"),
    QuerySpec("Goals", "Goal", "Goal", "patient/Goal.read"),
)
VENDOR = settings("v", TABLE, page_cap=10, max_retries=0)


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.reason = status, body, "x"

    def json(self):
        return self._body


class FakeEhr:
    """Labs: 3 pages of 2; Vitals overlaps Labs by one id; Goals 403; Patient instance read."""

    def __init__(self, goals_status=200, fail_after=None):
        self.headers = {}
        self.calls = []
        self.goals_status = goals_status
        self.fail_after = fail_after

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            return Resp(401, {})
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()} | (params or {})
        if u.path.endswith("/Patient/pat-1"):
            return Resp(200, {"resourceType": "Patient", "id": "pat-1"})
        assert q.get("patient") == "pat-1"
        if u.path.endswith("/Goal"):
            return Resp(self.goals_status, {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "nope"}]})
        if q.get("category") == "laboratory":
            page = int(q.get("page", 0))
            entries = [{"resource": {"resourceType": "Observation", "id": f"lab{page * 2 + i}"}} for i in range(2)]
            entries.append({"resource": {"resourceType": "OperationOutcome"}, "search": {"mode": "outcome"}})
            nxt = f"{BASE}/Observation?category=laboratory&patient=pat-1&page={page + 1}"
            links = [{"relation": "next", "url": nxt}]
            return Resp(200, {"resourceType": "Bundle", "entry": entries, "link": links if page < 2 else []})
        if q.get("category") == "vital-signs":
            entries = [{"resource": {"resourceType": "Observation", "id": i}} for i in ("lab0", "vs1")]
            return Resp(200, {"resourceType": "Bundle", "entry": entries})
        raise AssertionError(url)


def _job(expires=None):
    return Job(
        id="j",
        participant=Participant("p", "static"),
        source=source(selection(iss=BASE), scopes_granted=["patient/*.read"]),
        destinations=("d",),
        credentials=Secrets({EHR_TOKEN: "tok"}),
        token_expires_at=expires or datetime.now(UTC) + timedelta(hours=1),
    )


def _spool(job):
    return InMemorySpool().open(job, b"k" * 32)


def test_pages_dedupes_and_isolates_failures():
    job, ehr = _job(), FakeEhr(goals_status=403)
    spool = _spool(job)
    fetch(job, spool, VENDOR, lambda: None, http=ehr)
    out = {o.label: o for o in job.outcomes}
    assert (out["Demographics"].status, out["Demographics"].count) == ("ok", 1)
    assert (out["Labs"].count, out["Labs"].pages) == (6, 3)
    assert out["Vitals"].count == 1  # lab0 already spooled by Labs
    assert out["Goals"].status == "error" and "403 nope" in out["Goals"].error
    assert spool.index().types == {"Patient": 1, "Observation": 7}
    assert ehr.headers["Authorization"] == "Bearer tok"


def test_401_aborts_the_fetch():
    job = _job()
    with pytest.raises(FetchAborted):
        fetch(job, _spool(job), VENDOR, lambda: None, http=FakeEhr(fail_after=1))


def test_expiry_stops_cleanly_and_marks_the_rest():
    job = _job(expires=datetime.now(UTC) + timedelta(seconds=10))  # inside the 30 s margin
    fetch(job, _spool(job), VENDOR, lambda: None, http=FakeEhr())
    assert {o.status for o in job.outcomes} == {"error"}
    assert "expired" in job.outcomes[0].error


def test_resume_refetches_only_unfinished_types():
    job, ehr = _job(), FakeEhr(goals_status=403)
    spool = _spool(job)
    fetch(job, spool, VENDOR, lambda: None, http=ehr)
    ehr2 = FakeEhr(goals_status=403)
    fetch(job, spool, VENDOR, lambda: None, http=ehr2)
    assert [c[0].rsplit("/", 1)[-1] for c in ehr2.calls] == ["Goal"]  # Patient and Observation were done
    assert spool.index().types == {"Patient": 1, "Observation": 7}
