"""S3StagingSpool against the compose Silo (skipped when it isn't reachable)."""

import os
import socket
import uuid
from datetime import timedelta

import pytest

from patient_access.core.models import BlobMeta, Job, Participant
from patient_access.testing.memory import source


def _silo_up() -> bool:
    try:
        socket.create_connection(("pa-s3", 9000), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _silo_up(), reason="Silo (pa-s3) not running")


def _spool():
    from patient_access.spool.s3_staging import S3StagingSpool

    return S3StagingSpool(
        bucket="patient-access-tests",
        endpoint_url="http://pa-s3:9000",
        access_key="patient-access-dev",
        secret_key="patient-access-dev-secret",
        chunk=2,
        expire_days=1,
        create_bucket=True,
    )


def _job():
    return Job(
        id=f"t-{uuid.uuid4().hex[:8]}",
        participant=Participant("p", "s"),
        source=source(),
        destinations=("d",),
    )


def test_chunks_flush_resume_blobs_and_shred():
    spool, job, key = _spool(), _job(), os.urandom(32)
    h = spool.open(job, key)
    for i in range(5):
        h.append("Observation", {"resourceType": "Observation", "id": str(i), "note": "SECRET"})
    h.append("Goal", {"id": "g"})  # never flushed: lost with the process, by contract
    h.flush("Observation")
    h.put_blob("DocumentReference-d-0", b"secret pdf", BlobMeta("application/pdf", 10, "h"))
    h2 = spool.open(job, key)  # the first handle "crashed": no close()
    assert [r["id"] for r in h2.iter_type("Observation")] == ["0", "1", "2", "3", "4"]
    assert list(h2.iter_type("Goal")) == []
    assert h2.get_blob("DocumentReference-d-0") == b"secret pdf"
    raw = [
        spool.client.get_object(Bucket=spool.bucket, Key=o["Key"])["Body"].read()
        for o in spool.client.list_objects_v2(Bucket=spool.bucket, Prefix=f"staging/{job.id}/")["Contents"]
    ]
    assert raw and not any(b"SECRET" in b or b"secret pdf" in b for b in raw)
    assert spool.sweep(keep={job.id}, older_than=timedelta(0)) == []
    assert job.id in spool.sweep(keep=set(), older_than=timedelta(0))
    assert job.id not in spool._refs()
    rules = spool.client.get_bucket_lifecycle_configuration(Bucket=spool.bucket)["Rules"]
    assert any(r["ID"] == "expire-staging" and r["Expiration"]["Days"] == 1 for r in rules)
