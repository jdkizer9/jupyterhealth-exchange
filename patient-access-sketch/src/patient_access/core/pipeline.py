"""run_job and the Destination pairing (design §5, §7.2).

run_job(job_id, deps) loads the job, runs the phases it has not completed and saves after each.
It is resumable by phase: a retry after a delivery failure starts at `fetched` and never touches
the EHR again. Scheduling is not its concern; a JobDispatcher's worker side calls it.
"""

from __future__ import annotations

import base64
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .interfaces import Formatter, JobStore, Sink, Spool, SpoolHandle
from .models import Job, JobId, JobStatus, Receipt
from .vendors import ConfigError, VendorConfig

log = logging.getLogger(__name__)

SPOOL_KEY = "spool_key"
EHR_TOKEN = "ehr_access_token"


@dataclass
class Destination:
    """A Formatter and a Sink configured together; the pairing is checked at construction."""

    name: str
    formatter: Formatter
    sink: Sink

    def __post_init__(self) -> None:
        missing = set(self.formatter.produces) - set(self.sink.accepts)
        if missing:
            raise ConfigError(
                f"destination {self.name}: {type(self.formatter).__name__} produces {sorted(missing)}, "
                f"which {type(self.sink).__name__} does not accept"
            )


# fetch(job, spool, vendor, save) fills the spool and records outcomes on the job; `save` persists
# progress. Core's puller is the real one; tests pass a fake.
FetchFn = Callable[[Job, SpoolHandle, VendorConfig, Callable[[], None]], None]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    jobs: JobStore
    spool: Spool
    destinations: Mapping[str, Destination]
    vendors: Mapping[str, VendorConfig]
    fetch: FetchFn
    clock: Callable[[], datetime] = utcnow
    max_attempts: int = 3
    on_event: Callable[[Job, str], None] = field(default=lambda job, event: None)


class FetchAborted(Exception):
    """Phase 4 cannot continue (token expired, spool unusable). Terminal for the job."""


def run_job(job_id: JobId, deps: Deps) -> Job:
    job = deps.jobs.get(job_id)
    if job.status.terminal:
        return job
    job.attempts += 1
    if SPOOL_KEY not in job.credentials:
        job.credentials[SPOOL_KEY] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    handle = deps.spool.open(job, base64.urlsafe_b64decode(job.credentials[SPOOL_KEY]))
    job.spool_ref = handle.ref
    deps.jobs.save(job)

    try:
        if job.attempts > 1 and not _spool_intact(job, handle, deps):
            return job
        if job.status in (JobStatus.QUEUED, JobStatus.FETCHING):
            _fetch(job, handle, deps)
        if job.status in (JobStatus.FETCHED, JobStatus.DELIVERING, JobStatus.FAILED):
            _deliver(job, handle, deps)
    finally:
        handle.close()
    return job


def _spool_intact(job: Job, handle: SpoolHandle, deps: Deps) -> bool:
    """On resume, never trust the row's "ok" marks over the spool itself: a process or machine
    that died (or was redeployed) may have taken a local-disk spool with it. A type whose spool
    count disagrees with its outcomes is refetched if the token is still live; if the fetch had
    already finished and the token is gone, the job fails honestly rather than delivering a
    silently incomplete pull."""
    expected: dict[str, int] = {}
    for o in job.outcomes:
        if o.status == "ok":
            expected[o.resource_type] = expected.get(o.resource_type, 0) + o.count
    actual = handle.index().types
    lost = [t for t, n in expected.items() if n and actual.get(t, 0) != n]
    if not lost:
        return True
    if job.status in (JobStatus.QUEUED, JobStatus.FETCHING):
        log.warning("job %s: spool lost for %s; refetching", job.id, lost)
        for o in job.outcomes:
            if o.resource_type in lost:
                o.status, o.count, o.pages, o.bytes = "pending", 0, 0, 0
        for t in lost:
            handle.reset_type(t)
        deps.jobs.save(job)
        return True
    _fail_terminal(
        job,
        deps,
        "The downloaded records were lost before they could be delivered (the service restarted). "
        "Please connect again.",
    )
    return False


def _fetch(job: Job, handle: SpoolHandle, deps: Deps) -> None:
    now = deps.clock()
    if EHR_TOKEN not in job.credentials or (job.token_expires_at and job.token_expires_at <= now):
        _fail_terminal(job, deps, "The EHR authorization expired before the download finished. Please connect again.")
        return
    vendor = deps.vendors.get(job.source.vendor)
    if vendor is None:
        _fail_terminal(job, deps, f"vendor {job.source.vendor!r} is not configured")
        return
    job.status = JobStatus.FETCHING
    deps.jobs.save(job)
    deps.on_event(job, "fetch.start")
    try:
        deps.fetch(job, handle, vendor, lambda: deps.jobs.save(job))
    except FetchAborted as e:
        _fail_terminal(job, deps, str(e))
        return
    except Exception as e:  # noqa: BLE001 - anything else in the fetch is a bug or an outage; don't retry the EHR
        log.exception("job %s: fetch failed", job.id)
        _fail_terminal(job, deps, f"download failed: {e}")
        return
    # The token is no longer needed by anything after the fetch.
    job.credentials.pop(EHR_TOKEN)
    job.token_expires_at = None
    job.status = JobStatus.FETCHED
    deps.jobs.save(job)
    deps.on_event(job, "fetch.done")


def _deliver(job: Job, handle: SpoolHandle, deps: Deps) -> None:
    destination = deps.destinations.get(job.destination)
    if destination is None:
        _fail_terminal(job, deps, f"destination {job.destination!r} is not configured")
        return
    job.status = JobStatus.DELIVERING
    job.failure = None
    deps.jobs.save(job)
    deps.on_event(job, "deliver.start")
    session = destination.sink.begin(job)
    units = 0
    try:
        for unit in destination.formatter.format(handle, job):
            session.write(unit, handle)
            units += 1
        receipt: Receipt = session.finish()
    except Exception as e:  # noqa: BLE001 - the spool still has the data; retry later
        log.exception("job %s: delivery failed", job.id)
        try:
            session.abort(str(e))
        except Exception:  # noqa: BLE001
            log.exception("job %s: sink abort failed", job.id)
        if job.attempts >= deps.max_attempts:
            _fail_terminal(job, deps, f"delivery failed after {job.attempts} attempts: {e}")
        else:
            job.status = JobStatus.FAILED
            job.failure = f"delivery failed, will retry: {e}"
            deps.jobs.save(job)
        return
    job.receipt = receipt
    job.status = JobStatus.DONE
    _finish(job, deps)
    deps.on_event(job, "deliver.done")


def _fail_terminal(job: Job, deps: Deps, reason: str) -> None:
    job.status = JobStatus.FAILED_TERMINAL
    job.failure = reason
    _finish(job, deps)
    deps.on_event(job, "failed")


def _finish(job: Job, deps: Deps) -> None:
    """Terminal state: delete the spool (crypto-shred: the key goes with the credentials) and save."""
    if job.spool_ref:
        deps.spool.delete(job.spool_ref)
        job.spool_ref = None
    job.credentials.clear()
    job.participant.secrets.clear()  # e.g. the JHE token: never outlives the job
    job.token_expires_at = None
    job.finished_at = deps.clock()
    deps.jobs.save(job)
