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

from .interfaces import Directory, Formatter, JobStore, Sink, Spool, SpoolHandle
from .models import ContentPolicy, Delivery, Job, JobId, JobStatus, Receipt
from .source_settings import ConfigError, SourceSettings

log = logging.getLogger(__name__)

SPOOL_KEY = "spool_key"
EHR_TOKEN = "ehr_access_token"


@dataclass
class Destination:
    """A Formatter and a Sink configured together; the pairing is checked at construction.

    `required`: a required destination that gives up fails the job; a best-effort one fails on
    its own and the job can still finish. `content`: what this destination takes (§14)."""

    name: str
    formatter: Formatter
    sink: Sink
    required: bool = True
    content: ContentPolicy = field(default_factory=ContentPolicy)

    def __post_init__(self) -> None:
        missing = set(self.formatter.produces) - set(self.sink.accepts)
        if missing:
            raise ConfigError(
                f"destination {self.name}: {type(self.formatter).__name__} produces {sorted(missing)}, "
                f"which {type(self.sink).__name__} does not accept"
            )


# fetch(job, spool, settings, save) fills the spool and records outcomes on the job; `save` persists
# progress. Core's puller is the real one; tests pass a fake.
FetchFn = Callable[[Job, SpoolHandle, SourceSettings, Callable[[], None]], None]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    jobs: JobStore
    spool: Spool
    destinations: Mapping[str, Destination]
    directory: Directory  # for settings_for: the fetch profile is looked up per run, never stored
    fetch: FetchFn
    clock: Callable[[], datetime] = utcnow
    max_attempts: int = 3  # per destination
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
    token_live = EHR_TOKEN in job.credentials and (job.token_expires_at is None or job.token_expires_at > deps.clock())
    if token_live:
        log.warning("job %s: spool lost for %s; refetching", job.id, lost)
        job.status = JobStatus.FETCHING
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
        "The downloaded records were lost before they could be delivered and the authorization has "
        "expired. Please connect again.",
    )
    return False


def _fetch(job: Job, handle: SpoolHandle, deps: Deps) -> None:
    now = deps.clock()
    if EHR_TOKEN not in job.credentials or (job.token_expires_at and job.token_expires_at <= now):
        _fail_terminal(job, deps, "The EHR authorization expired before the download finished. Please connect again.")
        return
    try:
        settings = deps.directory.settings_for(job.source.endpoint)
    except KeyError:
        _fail_terminal(job, deps, f"vendor {job.source.vendor!r} is not configured")
        return
    job.status = JobStatus.FETCHING
    deps.jobs.save(job)
    deps.on_event(job, "fetch.start")
    try:
        deps.fetch(job, handle, settings, lambda: deps.jobs.save(job))
    except FetchAborted as e:
        _fail_terminal(job, deps, str(e))
        return
    except Exception as e:  # noqa: BLE001 - anything else in the fetch is a bug or an outage; don't retry the EHR
        log.exception("job %s: fetch failed", job.id)
        _fail_terminal(job, deps, f"download failed: {e}")
        return
    # The token stays on the row (encrypted) until the job finishes, not just until the fetch
    # does: if the spool is lost before delivery completes (a redeploy on ephemeral disk), a live
    # token means a refetch instead of asking the patient to authorize again. It expires within
    # the hour regardless, and _finish clears it.
    job.status = JobStatus.FETCHED
    deps.jobs.save(job)
    deps.on_event(job, "fetch.done")


def _deliver(job: Job, handle: SpoolHandle, deps: Deps) -> None:
    """Phases 5-6 for every destination (design §14): each gets its own formatter run, sink
    session, receipt and attempt count; one failing does not stop the next. Retry re-runs only
    the deliveries that failed. The spool outlives every delivery and is deleted at the end.
    The list of destinations is the one the launch recorded; nothing changes it in flight."""
    job.status = JobStatus.DELIVERING
    job.failure = None
    deps.jobs.save(job)
    deps.on_event(job, "deliver.start")
    for name in job.destinations:
        delivery = job.delivery(name)
        if delivery.terminal:
            continue
        destination = deps.destinations.get(name)
        if destination is None:
            delivery.status, delivery.failure = "failed_terminal", f"destination {name!r} is not configured"
            delivery.finished_at = deps.clock()
            deps.jobs.save(job)
            continue
        _deliver_one(job, handle, deps, destination, delivery)
        deps.jobs.save(job)
    _settle(job, deps)


def _deliver_one(job: Job, handle: SpoolHandle, deps: Deps, destination: Destination, delivery: Delivery) -> None:
    delivery.status, delivery.failure = "delivering", None
    delivery.attempts += 1
    delivery.started_at = delivery.started_at or deps.clock()
    deps.jobs.save(job)
    session = None
    try:
        # begin is inside the try: a sink that registers things up front (JHE: identifier,
        # FhirSource) fails like any other delivery error, not out of run_job mid-phase.
        session = destination.sink.begin(job)
        for unit in destination.formatter.format(handle, job, destination.content):
            session.write(unit, handle)
        receipt: Receipt = session.finish()
    except Exception as e:  # noqa: BLE001 - the spool still has the data; retry later
        log.exception("job %s: delivery to %s failed", job.id, destination.name)
        try:
            if session is not None:
                session.abort(str(e))
        except Exception:  # noqa: BLE001
            log.exception("job %s: sink abort failed", job.id)
        if delivery.attempts >= deps.max_attempts:
            delivery.status = "failed_terminal"
            delivery.failure = f"delivery failed after {delivery.attempts} attempts: {e}"
            delivery.finished_at = deps.clock()
        else:
            delivery.status, delivery.failure = "failed", f"delivery failed, will retry: {e}"
        return
    delivery.status, delivery.receipt, delivery.finished_at = "done", receipt, deps.clock()


def _settle(job: Job, deps: Deps) -> None:
    """The job's status from its deliveries. Required ones decide; best-effort ones only record."""

    def is_required(name: str) -> bool:
        d = deps.destinations.get(name)
        return d is None or d.required  # an unconfigured name was marked failed_terminal: required

    required = [job.delivery(n) for n in job.destinations if is_required(n)]
    gave_up = [d for d in required if d.status == "failed_terminal"]
    if gave_up:
        _fail_terminal(job, deps, "; ".join(d.failure or "" for d in gave_up))
        return
    if any(d.status == "failed" for d in job.deliveries.values()):
        job.status = JobStatus.FAILED
        job.failure = "; ".join(f"{n}: {d.failure}" for n, d in job.deliveries.items() if d.status == "failed")
        deps.jobs.save(job)
        return
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
