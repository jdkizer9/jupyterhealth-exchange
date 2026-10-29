"""Documents pass against a fake server: inline, Binary by URL, FHIR Binary JSON, caps, foreign hosts."""

import base64
import json
from datetime import UTC, datetime, timedelta

from patient_access.core.documents import fetch_documents
from patient_access.core.models import Job, Participant, Secrets
from patient_access.core.pipeline import EHR_TOKEN
from patient_access.core.puller import FhirClient
from patient_access.core.source_settings import DocumentPolicy
from patient_access.testing.memory import InMemorySpool, selection, settings, source

BASE = "https://ehr.example/fhir"


class Resp:
    def __init__(self, status, data=b"", ctype="application/octet-stream"):
        self.status_code = status
        self._data = data
        self.headers = {"Content-Type": ctype, "Content-Length": str(len(data))}

    def iter_content(self, n):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]

    def close(self):
        pass


class FakeEhr:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, headers=None, timeout=None, stream=False, params=None):
        self.calls.append(url)
        if url.endswith("/Binary/pdf"):
            return Resp(200, b"%PDF-1.7 tiny", "application/pdf")
        if url.endswith("/Binary/json"):
            data = base64.b64encode(b"<p>hi</p>").decode()
            body = {"resourceType": "Binary", "contentType": "text/html", "data": data}
            return Resp(200, json.dumps(body).encode(), "application/fhir+json")
        if url.endswith("/Binary/big"):
            return Resp(200, b"x" * 2000, "application/pdf")
        return Resp(404)


def _setup(policy):
    vendor = settings("v", documents=policy).fetch
    job = Job(
        id="j",
        participant=Participant("p", "s"),
        source=source(selection(iss=BASE), ehr_patient_id="pat"),
        destinations=("d",),
        credentials=Secrets({EHR_TOKEN: "t"}),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    spool = InMemorySpool().open(job, b"k" * 32)

    def doc(rid, *atts):
        spool.append(
            "DocumentReference",
            {"resourceType": "DocumentReference", "id": rid, "content": [{"attachment": a} for a in atts]},
        )

    doc("inline", {"contentType": "text/plain", "data": base64.b64encode(b"note").decode()})
    doc("pick", {"contentType": "application/pdf", "url": "Binary/pdf"}, {"contentType": "text/rtf", "url": "Binary/x"})
    doc("json", {"contentType": "text/html", "url": f"{BASE}/Binary/json"})
    doc("foreign", {"contentType": "application/pdf", "url": "https://elsewhere.example/doc.pdf"})
    doc("big", {"contentType": "application/pdf", "url": "Binary/big"})
    spool.append(
        "DiagnosticReport",
        {
            "resourceType": "DiagnosticReport",
            "id": "dr",
            "presentedForm": [{"contentType": "application/pdf", "url": "Binary/pdf"}],
        },
    )
    ehr = FakeEhr()
    client = FhirClient(BASE, "t", vendor, job.token_expires_at, http=ehr)
    return job, spool, vendor, client, ehr


def test_documents_pass():
    job, spool, vendor, client, ehr = _setup(DocumentPolicy(max_bytes_each=1000))
    fetch_documents(job, spool, vendor, client, lambda: None)
    d = job.documents
    assert (d.status, d.candidates, d.fetched, d.inline) == ("ok", 6, 4, 1)
    assert d.skipped == {"url outside the FHIR server": 1, "over per-document size cap": 1}
    blobs = spool.index().blobs
    assert set(blobs) == {
        "DocumentReference-inline-0",
        "DocumentReference-pick-0",
        "DocumentReference-json-0",
        "DiagnosticReport-dr-0",
    }
    assert spool.get_blob("DocumentReference-json-0") == b"<p>hi</p>"
    assert blobs["DocumentReference-json-0"].content_type == "text/html"
    assert not any("elsewhere" in c for c in ehr.calls)  # the token never left the FHIR server
    assert f"{BASE}/Binary/x" not in ehr.calls  # rtf alternative not fetched: pdf preferred


def test_count_and_total_caps():
    job, spool, vendor, client, _ = _setup(DocumentPolicy(max_count=2, max_bytes_each=5000, max_total_bytes=20))
    fetch_documents(job, spool, vendor, client, lambda: None)
    assert job.documents.fetched == 2
    assert job.documents.skipped["over document count cap"] >= 1
