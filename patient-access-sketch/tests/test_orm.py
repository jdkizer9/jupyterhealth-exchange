"""The Django edge: rows round-trip to dataclasses, secrets are encrypted, the claimer leases."""

from datetime import UTC, datetime, timedelta

import pytest

from patient_access.core.models import Job, JobStatus, Participant, Receipt, Secrets, Source, TypeOutcome
from patient_access.django_app.models import JobRecord
from patient_access.django_app.stores import OrmJobClaimer, OrmJobStore

pytestmark = pytest.mark.django_db


def _job(**kw):
    return Job(
        id="",
        participant=Participant(ref="p1", provider="static", attrs={"arm": "a"}, secrets=Secrets({"jhe": "t0k"})),
        source=Source("v", "https://ehr/fhir", "Brand", "1", "Clinic", ["s"], ["s"], "pat-1"),
        destination="local",
        **kw,
    )


def test_round_trip_and_encryption():
    store = OrmJobStore()
    job = _job(credentials=Secrets({"ehr_access_token": "SECRET-EHR"}))
    job_id = store.create(job)
    row = JobRecord.objects.get(id=job_id)
    assert "SECRET-EHR" not in row.credentials_enc and "t0k" not in row.participant_secrets_enc
    got = store.get(job_id)
    assert got.credentials["ehr_access_token"] == "SECRET-EHR"
    assert got.participant.secrets["jhe"] == "t0k"
    got.outcomes = [TypeOutcome("Labs", "Observation", "Observation?category=laboratory", "ok", 3)]
    got.receipt = Receipt(destination="local", location="x", resources=3)
    got.status = JobStatus.DONE
    got.finished_at = datetime.now(UTC)
    store.save(got)
    again = store.get(job_id)
    assert again.status == JobStatus.DONE and again.outcomes[0].count == 3 and again.receipt.resources == 3


def test_claimer_leases_once_and_reclaims_stale():
    store, claimer = OrmJobStore(), OrmJobClaimer()
    job_id = store.create(_job())
    assert claimer.claim_next("w1", timedelta(minutes=10)) == job_id
    assert claimer.claim_next("w2", timedelta(minutes=10)) is None  # leased
    JobRecord.objects.filter(id=job_id).update(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert claimer.claim_next("w2", timedelta(minutes=10)) == job_id  # stale lease: w1 died
    claimer.release(job_id, "w1")  # w1's late release must not clear w2's lease
    assert JobRecord.objects.get(id=job_id).lease_owner == "w2"


def test_save_does_not_touch_the_lease():
    store, claimer = OrmJobStore(), OrmJobClaimer()
    job_id = store.create(_job())
    claimer.claim_next("w1", timedelta(minutes=10))
    job = store.get(job_id)
    job.status = JobStatus.FETCHING
    store.save(job)
    assert JobRecord.objects.get(id=job_id).lease_owner == "w1"
