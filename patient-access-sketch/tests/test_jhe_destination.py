"""R4BatchFormatter chunking and JheHttpSink's per-entry tally, against a fake JHE."""

from patient_access.core.models import Job, Participant, Secrets
from patient_access.destinations import jhe as jhe_mod
from patient_access.destinations.jhe import JheHttpSink
from patient_access.formatters.r4_batch import R4BatchFormatter
from patient_access.identity.jhe_invitation import JHE_TOKEN
from patient_access.testing.memory import InMemorySpool, selection, source


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""
        self.request = type("R", (), {"body": b"{}"})()

    def json(self):
        return self._body


class FakeJhe:
    def __init__(self):
        self.headers = {}
        self.posts = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.posts.append(url.rsplit("/", 2)[-2] if url.endswith("/") else url.rsplit("/", 1)[-1])
        if url.endswith("identifier"):
            return Resp(200)
        if url.endswith("fhir_sources"):
            return Resp(200, {"id": 7})
        if url.endswith("revoke_token/"):
            return Resp(200)
        entries = []
        for i, _ in enumerate(json["entry"]):
            if i == 0:
                outcome = {"issue": [{"severity": "error", "diagnostics": "duplicate"}]}
                entries.append({"response": {"status": "409", "outcome": outcome}})
            else:
                outcome = {"issue": [{"severity": "warning", "diagnostics": "dropped x"}]}
                entries.append({"response": {"status": "201", "outcome": outcome}})
        return Resp(200, {"resourceType": "Bundle", "type": "batch-response", "entry": entries})


def test_batches_and_tally(monkeypatch):
    fake = FakeJhe()
    monkeypatch.setattr(jhe_mod, "http_session", lambda: fake)
    job = Job(
        id="j",
        participant=Participant(
            "40001", "jhe_invitation", attrs={"jhe_client_id": "c"}, secrets=Secrets({JHE_TOKEN: "tok"})
        ),
        source=source(selection(iss="https://ehr/fhir", brand="B", facility="L"), ehr_patient_id="pat"),
        destination="jhe",
    )
    spool = InMemorySpool().open(job, b"k" * 32)
    for i in range(250):
        spool.append("Observation", {"resourceType": "Observation", "id": str(i)})
    units = list(R4BatchFormatter(chunk=100).format(spool, job))
    assert [u.resources for u in units] == [100, 100, 50] and units[0].kind == "batch"
    session = JheHttpSink("http://jhe:8000", 70006).begin(job)
    for u in units:
        session.write(u, spool)
    receipt = session.finish()
    assert receipt.resources == 247 and receipt.failures == {"duplicate": 3}
    assert receipt.warnings == {"dropped x": 247}
    assert receipt.details["fhir_source_id"] == 7 and receipt.details["jhe_token_revoked"]
    assert fake.headers["Authorization"] == "Bearer tok"
