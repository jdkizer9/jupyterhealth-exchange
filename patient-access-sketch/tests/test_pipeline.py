"""run_job against in-memory plugs: the phase walker, resume, retry, crypto-shred bookkeeping."""

from datetime import UTC, datetime, timedelta

import pytest

from patient_access.core.models import Job, JobStatus, Participant, TypeOutcome
from patient_access.core.pipeline import EHR_TOKEN, SPOOL_KEY, Deps, Destination, FetchAborted, run_job
from patient_access.core.source_settings import ConfigError, QuerySpec
from patient_access.testing.memory import (
    InMemoryDirectory,
    InMemoryJobStore,
    InMemorySpool,
    MemorySink,
    PassthroughFormatter,
    settings,
    source,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

SETTINGS = settings(
    "v",
    (QuerySpec(label="Conditions", resource_type="Condition", query="Condition", scope="patient/Condition.read"),),
    base_scopes=("launch/patient",),
)


def fake_fetch(job, spool, settings, save):
    outcome = TypeOutcome(label="Conditions", resource_type="Condition", query="Condition")
    job.outcomes = [outcome]
    for i in range(3):
        outcome.bytes += spool.append("Condition", {"resourceType": "Condition", "id": f"c{i}"})
        outcome.count += 1
    outcome.status = "ok"
    save()


def make(sink=None, fetch=fake_fetch, expires=NOW + timedelta(hours=1), extra=None):
    """One destination "mem"; `extra` adds more {name: Destination}."""
    jobs, spool, sink = InMemoryJobStore(), InMemorySpool(), sink or MemorySink()
    destinations = {"mem": Destination("mem", PassthroughFormatter(), sink), **(extra or {})}
    deps = Deps(
        jobs=jobs,
        spool=spool,
        destinations=destinations,
        directory=InMemoryDirectory(settings={"v": SETTINGS}),
        fetch=fetch,
        clock=lambda: NOW,
    )
    job = Job(
        id="j1",
        participant=Participant(ref="p1", provider="static"),
        source=source(),
        destinations=("mem", *(extra or {})),
        token_expires_at=expires,
    )
    job.credentials[EHR_TOKEN] = "tok"
    jobs.create(job)
    return deps, jobs, spool, sink


def test_happy_path_walks_every_phase_and_shreds():
    deps, jobs, spool, sink = make()
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE
    assert job.deliveries["mem"].receipt.resources == 3
    assert [u.name for u in sink.delivered["j1"]] == ["Condition"]
    stored = jobs.get("j1")
    assert not stored.credentials  # EHR token and spool key gone: the spool is unreadable even if left behind
    assert stored.spool_ref is None and spool.deleted == ["mem-j1"]


def test_token_is_kept_until_the_job_finishes():
    seen = {}
    deps, jobs, spool, sink = make()
    sink_begin = sink.begin

    def spy_begin(job):
        seen["has_token"] = EHR_TOKEN in jobs.get(job.id).credentials
        seen["has_key"] = SPOOL_KEY in jobs.get(job.id).credentials
        return sink_begin(job)

    sink.begin = spy_begin
    run_job("j1", deps)
    assert seen == {"has_token": True, "has_key": True}  # a lost spool during delivery can still refetch
    assert not jobs.get("j1").credentials  # and nothing survives the end


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


def test_sink_begin_failure_is_a_retryable_delivery_failure():
    class BeginFails(MemorySink):
        def begin(self, job):
            raise RuntimeError("identifier already belongs to another patient")

    deps, jobs, spool, _ = make(sink=BeginFails())
    job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED and "another patient" in job.failure
    assert spool.spools  # kept for the retry


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


def test_lost_spool_during_fetch_is_refetched():
    calls = {"fetch": 0}

    def counting_fetch(*a):
        calls["fetch"] += 1
        fake_fetch(*a)

    deps, jobs, spool, sink = make(fetch=counting_fetch)
    # First attempt: fetch finished, then the process died before delivery began...
    job = jobs.get("j1")
    job.attempts = 1
    job.status = JobStatus.FETCHING
    job.outcomes = [TypeOutcome("Conditions", "Condition", "Condition", status="ok", count=3)]
    job.credentials[SPOOL_KEY] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    jobs.save(job)
    # ...and the machine was replaced: the spool is empty but the row says Conditions are done.
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE and calls["fetch"] == 1 and job.deliveries["mem"].receipt.resources == 3


def test_lost_spool_after_fetch_refetches_while_the_token_lives():
    calls = {"fetch": 0}

    def counting_fetch(*a):
        calls["fetch"] += 1
        fake_fetch(*a)

    deps, jobs, spool, sink = make(fetch=counting_fetch)
    job = jobs.get("j1")
    job.attempts = 1
    job.status = JobStatus.FETCHED  # fetch done, then the machine was replaced before delivery
    job.credentials[SPOOL_KEY] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    job.outcomes = [TypeOutcome("Conditions", "Condition", "Condition", status="ok", count=3)]
    jobs.save(job)
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE and calls["fetch"] == 1 and job.deliveries["mem"].receipt.resources == 3


def test_lost_spool_with_expired_token_fails_honestly():
    deps, jobs, spool, sink = make()
    job = jobs.get("j1")
    job.attempts = 1
    job.status = JobStatus.FETCHED
    job.token_expires_at = NOW - timedelta(seconds=1)  # more than an hour went by
    job.credentials[SPOOL_KEY] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    job.outcomes = [TypeOutcome("Conditions", "Condition", "Condition", status="ok", count=3)]
    jobs.save(job)
    job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED_TERMINAL and "connect again" in job.failure
    assert sink.delivered == {}  # nothing incomplete went out


# --- fan-out (design §14) ------------------------------------------------------------------------


def _two(fail_second=None, required_second=True):
    """ "mem" plus a second destination "s3" (a MemorySink too), failing on `fail_second` if given."""
    second = MemorySink(fail_on=fail_second)
    extra = {"s3": Destination("s3", PassthroughFormatter(), second, required=required_second)}
    deps, jobs, spool, first = make(extra=extra)
    return deps, jobs, spool, first, second


def test_every_destination_gets_its_own_delivery_and_receipt():
    deps, jobs, spool, first, second = _two()
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE
    assert {n: d.status for n, d in job.deliveries.items()} == {"mem": "done", "s3": "done"}
    assert job.deliveries["mem"].receipt.resources == job.deliveries["s3"].receipt.resources == 3
    assert "j1" in first.delivered and "j1" in second.delivered
    assert not spool.spools  # deleted only after the last delivery


def test_one_failing_does_not_stop_the_other_and_retry_touches_only_it():
    deps, jobs, spool, first, second = _two(fail_second="Condition")
    job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED and "s3:" in job.failure
    assert job.deliveries["mem"].status == "done" and job.deliveries["s3"].status == "failed"
    assert spool.spools  # kept for the retry
    first_units = len(first.delivered["j1"])
    second.fail_on = None
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE
    assert len(first.delivered["j1"]) == first_units  # mem was not delivered twice
    assert job.deliveries["s3"].attempts == 2 and job.deliveries["mem"].attempts == 1
    assert not spool.spools


def test_best_effort_destination_fails_alone():
    deps, jobs, spool, first, second = _two(fail_second="Condition", required_second=False)
    for _ in range(3):
        job = run_job("j1", deps)
    assert job.status == JobStatus.DONE
    assert job.deliveries["mem"].status == "done" and job.deliveries["s3"].status == "failed_terminal"
    assert not spool.spools


def test_required_destination_giving_up_fails_the_job():
    deps, jobs, spool, first, second = _two(fail_second="Condition")
    for _ in range(3):
        job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED_TERMINAL and job.deliveries["mem"].status == "done"
    assert not spool.spools


def test_content_policy_narrows_what_a_destination_gets():
    from patient_access.core.models import ContentPolicy

    second = MemorySink()
    extra = {
        "narrow": Destination(
            "narrow", PassthroughFormatter(), second, content=ContentPolicy(exclude_types=frozenset({"Condition"}))
        )
    }
    deps, jobs, spool, first = make(extra=extra)
    job = run_job("j1", deps)
    assert job.status == JobStatus.DONE
    assert [u.name for u in first.delivered["j1"]] == ["Condition"]
    assert second.delivered["j1"] == []  # the only type was excluded


def test_unconfigured_destination_is_terminal_for_that_delivery():
    deps, jobs, spool, sink = make()
    job = jobs.get("j1")
    job.destinations = ("mem", "nope")
    jobs.save(job)
    job = run_job("j1", deps)
    assert job.status == JobStatus.FAILED_TERMINAL and job.deliveries["mem"].status == "done"
    assert "nope" in job.deliveries["nope"].failure
