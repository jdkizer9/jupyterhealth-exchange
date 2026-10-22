"""run_job against in-memory plugs: the phase walker, resume, retry, crypto-shred bookkeeping."""

from datetime import UTC, datetime, timedelta

import pytest

from patient_access.core.models import Job, JobStatus, Participant, Source, TypeOutcome
from patient_access.core.pipeline import EHR_TOKEN, SPOOL_KEY, Deps, Destination, FetchAborted, run_job
from patient_access.core.vendors import ConfigError, QuerySpec, VendorConfig
from patient_access.testing.memory import InMemoryJobStore, InMemorySpool, MemorySink, PassthroughFormatter

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

VENDOR = VendorConfig(
    name="v",
    client_id="c",
    auth_method="public",
    base_scopes=("launch/patient",),
    queries=(
        QuerySpec(label="Conditions", resource_type="Condition", query="Condition", scope="patient/Condition.read"),
    ),
)


def fake_fetch(job, spool, vendor, save):
    outcome = TypeOutcome(label="Conditions", resource_type="Condition", query="Condition")
    job.outcomes = [outcome]
    for i in range(3):
        outcome.bytes += spool.append("Condition", {"resourceType": "Condition", "id": f"c{i}"})
        outcome.count += 1
    outcome.status = "ok"
    save()


def make(sink=None, fetch=fake_fetch, expires=NOW + timedelta(hours=1)):
    jobs, spool, sink = InMemoryJobStore(), InMemorySpool(), sink or MemorySink()
    deps = Deps(
        jobs=jobs,
        spool=spool,
        destinations={"mem": Destination("mem", PassthroughFormatter(), sink)},
        vendors={"v": VENDOR},
        fetch=fetch,
        clock=lambda: NOW,
    )
    job = Job(
        id="j1",
        participant=Participant(ref="p1", provider="static"),
        source=Source("v", "https://ehr/fhir", "Brand", "1", "Clinic", [], [], "pat-1"),
        destination="mem",
        token_expires_at=expires,
    )
    job.credentials[EHR_TOKEN] = "tok"
    jobs.create(job)
    return deps, jobs, spool, sink


def test_happy_path_walks_every_phase_and_shreds():
    deps, jobs, spool, sink = make()
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE
    assert job.receipt.resources == 3
    assert [u.name for u in sink.delivered["j1"]] == ["Condition"]
    stored = jobs.get("j1")
    assert not stored.credentials  # EHR token and spool key gone: the spool is unreadable even if left behind
    assert stored.spool_ref is None and spool.deleted == ["mem-j1"]


def test_token_is_dropped_right_after_fetch():
    seen = {}
    deps, jobs, spool, sink = make()
    sink_begin = sink.begin

    def spy_begin(job):
        seen["has_token"] = EHR_TOKEN in jobs.get(job.id).credentials
        seen["has_key"] = SPOOL_KEY in jobs.get(job.id).credentials
        return sink_begin(job)

    sink.begin = spy_begin
    run_job("j1", deps)
    assert seen == {"has_token": False, "has_key": True}


def test_delivery_failure_keeps_spool_and_resumes_without_refetch():
    calls = {"fetch": 0}

    def counting_fetch(*a):
        calls["fetch"] += 1
        fake_fetch(*a)

    sink = MemorySink(fail_on="Condition")
    deps, jobs, spool, _ = make(sink=sink, fetch=counting_fetch)
    job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED and "will retry" in job.failure
    assert spool.spools  # data still there
    sink.fail_on = None
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE
    assert calls["fetch"] == 1


def test_delivery_gives_up_after_max_attempts():
    deps, jobs, spool, _ = make(sink=MemorySink(fail_on="Condition"))
    for _ in range(3):
        job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED_TERMINAL
    assert not spool.spools


def test_expired_token_is_terminal_before_any_fetch():
    deps, jobs, spool, _ = make(expires=NOW - timedelta(seconds=1))
    job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED_TERMINAL and "expired" in job.failure


def test_fetch_abort_is_terminal():
    def aborting(*a):
        raise FetchAborted("token rejected")

    deps, *_ = make(fetch=aborting)
    assert run_job("j1", deps).status == JobStatus.FAILED_TERMINAL


def test_terminal_job_is_left_alone():
    deps, jobs, *_ = make()
    run_job("j1", deps)
    saves = jobs.saves
    run_job("j1", deps)
    assert jobs.saves == saves


def test_destination_pairing_checked_at_construction():
    class OnlyBatch:
        accepts = frozenset({"batch"})

    with pytest.raises(ConfigError):
        Destination("bad", PassthroughFormatter(), OnlyBatch())
