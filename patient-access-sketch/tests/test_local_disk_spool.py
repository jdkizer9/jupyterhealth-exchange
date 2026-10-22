import os
from datetime import timedelta

from patient_access.core.models import BlobMeta, Job, Participant, Source
from patient_access.spool.local_disk import LocalDiskSpool


def _job():
    return Job(
        id="job-1",
        participant=Participant(ref="p", provider="static"),
        source=Source("v", "i", "b", "1", "l", [], [], "pat"),
        destination="d",
    )


def test_round_trip_and_nothing_readable_on_disk(tmp_path):
    spool = LocalDiskSpool(str(tmp_path))
    key = os.urandom(32)
    h = spool.open(_job(), key)
    h.append("Condition", {"resourceType": "Condition", "id": "c1", "code": {"text": "Asthma"}})
    h.put_blob("doc-1", b"%PDF secret note", BlobMeta("application/pdf", 16, "x"))
    assert list(h.iter_type("Condition"))[0]["id"] == "c1"
    assert h.get_blob("doc-1") == b"%PDF secret note"
    assert h.index().types == {"Condition": 1}
    h.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    assert b"Asthma" not in raw and b"secret note" not in raw


def test_reopen_with_same_key_resumes(tmp_path):
    spool = LocalDiskSpool(str(tmp_path))
    key = os.urandom(32)
    h = spool.open(_job(), key)
    h.append("Condition", {"id": "c1"})
    h.close()
    h2 = spool.open(_job(), key)
    assert h2.has_resource("Condition", "c1")
    h2.reset_type("Condition")
    assert h2.index().types == {}


def test_flushed_data_survives_a_crash(tmp_path):
    spool = LocalDiskSpool(str(tmp_path))
    key = os.urandom(32)
    h = spool.open(_job(), key)
    h.append("Condition", {"id": "c1"})
    h.flush("Condition")
    # The worker dies here: no close(). A new process reopens the spool.
    assert [r["id"] for r in spool.open(_job(), key).iter_type("Condition")] == ["c1"]


def test_sweep_keeps_live_spools(tmp_path):
    spool = LocalDiskSpool(str(tmp_path))
    (tmp_path / "live").mkdir()
    (tmp_path / "orphan").mkdir()
    old = 0
    os.utime(tmp_path / "live", (old, old))
    os.utime(tmp_path / "orphan", (old, old))
    assert spool.sweep(keep={"live"}, older_than=timedelta(hours=1)) == ["orphan"]
    assert (tmp_path / "live").exists()
