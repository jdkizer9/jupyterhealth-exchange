"""The Django edge: rows round-trip to dataclasses, secrets are encrypted, the claimer leases."""

from datetime import UTC, datetime, timedelta

import pytest

from patient_access.core.models import Job, JobStatus, Participant, Receipt, Secrets, TypeOutcome
from patient_access.django_apps.jobs.models import JobRecord
from patient_access.django_apps.jobs.stores import OrmJobClaimer, OrmJobStore
from patient_access.testing.memory import source

pytestmark = pytest.mark.django_db


def _job(**kw):
    return Job(
        id="",
        participant=Participant(ref="p1", provider="static", attrs={"arm": "a"}, secrets=Secrets({"jhe": "t0k"})),
        source=source(scopes_requested=["s"], scopes_granted=["s"]),
        destinations=("local",),
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
    got.delivery("local").receipt = Receipt(destination="local", location="x", resources=3)
    got.delivery("local").status = "done"
    got.status = JobStatus.DONE
    got.finished_at = datetime.now(UTC)
    store.save(got)
    again = store.get(job_id)
    assert again.status == JobStatus.DONE and again.outcomes[0].count == 3
    assert again.deliveries["local"].status == "done" and again.deliveries["local"].receipt.resources == 3


def test_claimer_leases_once_and_reclaims_stale():
    store, claimer = OrmJobStore(), OrmJobClaimer()
    job_id = store.create(_job())
    assert claimer.claim_next("w1", timedelta(minutes=10)) == job_id
    assert claimer.claim_next("w2", timedelta(minutes=10)) is None  # leased
    JobRecord.objects.filter(id=job_id).update(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert claimer.claim_next("w2", timedelta(minutes=10)) == job_id  # stale lease: w1 died
    claimer.release(job_id, "w1")  # w1's late release must not clear w2's lease
    assert JobRecord.objects.get(id=job_id).lease_owner == "w2"


def test_abandoned_mid_phase_job_with_released_lease_is_claimable():
    store, claimer = OrmJobStore(), OrmJobClaimer()
    job_id = store.create(_job())
    assert claimer.claim_next("w1", timedelta(minutes=10)) == job_id
    job = store.get(job_id)
    job.status = JobStatus.FETCHING
    store.save(job)
    claimer.release(job_id, "w1")  # a deploy: the thread's lease released at process exit
    assert claimer.claim_next("w2", timedelta(minutes=10)) == job_id


def test_save_does_not_touch_the_lease():
    store, claimer = OrmJobStore(), OrmJobClaimer()
    job_id = store.create(_job())
    claimer.claim_next("w1", timedelta(minutes=10))
    job = store.get(job_id)
    job.status = JobStatus.FETCHING
    store.save(job)
    assert JobRecord.objects.get(id=job_id).lease_owner == "w1"


def test_encryption_keys_rotate(settings):
    """Prepending a new key re-encrypts new writes while old rows still decrypt."""
    from cryptography.fernet import Fernet, InvalidToken

    old_keys = list(settings.PATIENT_ACCESS["ENCRYPTION_KEYS"])
    store = OrmJobStore()
    job_id = store.create(_job(credentials=Secrets({"ehr_access_token": "OLD"})))
    before = JobRecord.objects.get(id=job_id).credentials_enc

    new_key = Fernet.generate_key().decode()
    settings.PATIENT_ACCESS = {**settings.PATIENT_ACCESS, "ENCRYPTION_KEYS": [new_key, *old_keys]}
    assert store.get(job_id).credentials["ehr_access_token"] == "OLD"  # old key still decrypts
    job2 = store.get(job_id)
    store.save(job2)
    after = JobRecord.objects.get(id=job_id).credentials_enc
    assert after != before  # re-saved under the new first key
    assert store.get(job_id).credentials["ehr_access_token"] == "OLD"

    settings.PATIENT_ACCESS = {**settings.PATIENT_ACCESS, "ENCRYPTION_KEYS": old_keys}
    with pytest.raises(InvalidToken):
        store.get(job_id)  # the new key is gone; the row written under it is unreadable
