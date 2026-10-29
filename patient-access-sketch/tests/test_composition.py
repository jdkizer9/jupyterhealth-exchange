"""CompositionBundleFormatter + LocalDirectorySink, through run_job, with in-memory everything else."""

import json
from datetime import UTC, datetime, timedelta

from patient_access.core.models import BlobMeta, Job, Participant, Secrets, TypeOutcome
from patient_access.core.pipeline import EHR_TOKEN, Deps, Destination, run_job
from patient_access.destinations.local_dir import LocalDirectorySink
from patient_access.formatters.composition import ORIGINAL_URL_EXT, CompositionBundleFormatter, urn
from patient_access.testing.memory import (
    InMemoryDirectory,
    InMemoryJobStore,
    InMemorySpool,
    selection,
    settings,
    source,
)

ISS = "https://ehr.example/fhir"


def fake_fetch(job, spool, settings, save):
    spool.append("Patient", {"resourceType": "Patient", "id": "pat-1"})
    spool.append(
        "Observation",
        {
            "resourceType": "Observation",
            "id": "o1",
            "subject": {"reference": "Patient/pat-1"},
            "performer": [{"reference": "Practitioner/dr-9"}],
        },
    )
    spool.append(
        "DocumentReference",
        {
            "resourceType": "DocumentReference",
            "id": "d1",
            "subject": {"reference": f"{ISS}/Patient/pat-1"},
            "content": [{"attachment": {"contentType": "text/plain", "url": f"{ISS}/Binary/b1"}}],
        },
    )
    spool.put_blob(
        "DocumentReference-d1-0",
        b"note text",
        BlobMeta("text/plain", 9, "abc", f"{ISS}/Binary/b1", "DocumentReference/d1"),
    )
    job.outcomes = [TypeOutcome("x", "Observation", "Observation", "ok", 1)]


def test_document_bundle_on_disk(tmp_path):
    jobs = InMemoryJobStore()
    deps = Deps(
        jobs=jobs,
        spool=InMemorySpool(),
        directory=InMemoryDirectory(settings={"v": settings("v")}),
        fetch=fake_fetch,
        destinations={"local": Destination("local", CompositionBundleFormatter(), LocalDirectorySink(str(tmp_path)))},
    )
    jobs.create(
        Job(
            id="j1",
            participant=Participant("P/../1", "static", attrs={"arm": "A"}),
            source=source(selection(iss=ISS)),
            destinations=("local",),
            credentials=Secrets({EHR_TOKEN: "t"}),
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    job = run_job("j1", deps)
    assert job.status == "done", job.failure
    out = tmp_path / "pulls" / "P_.._1" / "j1"  # the ref can't escape the root
    bundle = json.loads((out / "bundle.json").read_text())
    assert bundle["type"] == "document" and bundle["entry"][0]["resource"]["resourceType"] == "Composition"
    by_type = {e["resource"]["resourceType"]: e["resource"] for e in bundle["entry"]}
    pat = urn("Patient", "pat-1")
    assert by_type["Observation"]["subject"]["reference"] == pat
    assert by_type["DocumentReference"]["subject"]["reference"] == pat  # absolute EHR URL resolved too
    assert by_type["Observation"]["performer"][0]["reference"] == f"{ISS}/Practitioner/dr-9"  # not pulled: absolute
    att = by_type["DocumentReference"]["content"][0]["attachment"]
    assert att["url"] == "docs/DocumentReference-d1-0"
    assert att["extension"] == [{"url": ORIGINAL_URL_EXT, "valueUrl": f"{ISS}/Binary/b1"}]
    assert (out / "docs" / "DocumentReference-d1-0").read_bytes() == b"note text"
    meta = json.loads((out / "metadata.json").read_text())
    assert meta["participant"]["attrs"] == {"arm": "A"} and meta["documents"]["items"][0]["sha256"] == "abc"
    receipt = job.deliveries["local"].receipt
    assert receipt.blobs == 1 and receipt.resources == 3
