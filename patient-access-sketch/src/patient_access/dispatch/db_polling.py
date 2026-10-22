"""DbPollingDispatcher: the `queued` row is the queue (design §7.2).

dispatch() does nothing. The worker side is `poll_forever`, which the host runs (here,
`manage.py run_jobs`). Claiming needs the database, so it goes through a small JobClaimer
Protocol that the Django app implements; this module stays framework-free.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol

from patient_access.core.interfaces import JobDispatcher, Spool
from patient_access.core.models import JobId

log = logging.getLogger(__name__)


class JobClaimer(Protocol):
    def claim_next(self, worker_id: str, lease: timedelta) -> JobId | None:
        """Atomically take one runnable job (queued, fetched, retryable failed, or a stale lease)."""
        ...

    def release(self, job_id: JobId, worker_id: str) -> None: ...

    def live_spool_refs(self) -> set[str]:
        """Spools still needed: those of non-terminal jobs."""
        ...


class DbPollingDispatcher(JobDispatcher):
    def __init__(
        self,
        interval_seconds: float = 2.0,
        lease_seconds: int = 600,
        sweep_every: int = 10,
        spool_max_age_hours: float = 24.0,
    ):
        self.interval = interval_seconds
        self.lease = timedelta(seconds=lease_seconds)
        self.sweep_every = sweep_every
        self.spool_max_age = timedelta(hours=spool_max_age_hours)

    def dispatch(self, job_id: JobId) -> None:
        log.info("job %s queued; a polling worker will claim it", job_id)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def poll_once(claimer: JobClaimer, run: Callable[[JobId], Any], lease: timedelta, wid: str) -> JobId | None:
    job_id = claimer.claim_next(wid, lease)
    if job_id is None:
        return None
    try:
        run(job_id)
    except Exception:  # noqa: BLE001 - run_job records failures on the row; a crash here must not kill the loop
        log.exception("job %s: run_job raised", job_id)
    finally:
        claimer.release(job_id, wid)
    return job_id


def sweep(claimer: JobClaimer, spool: Spool, max_age: timedelta) -> list[str]:
    gone = spool.sweep(keep=claimer.live_spool_refs(), older_than=max_age)
    if gone:
        log.warning("swept %d orphaned spool(s): %s", len(gone), gone)
    return gone


def poll_forever(
    dispatcher: DbPollingDispatcher,
    claimer: JobClaimer,
    run: Callable[[JobId], Any],
    spool: Spool,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    wid = worker_id()
    polls = 0
    log.info("worker %s polling every %ss", wid, dispatcher.interval)
    sweep(claimer, spool, dispatcher.spool_max_age)
    while not should_stop():
        ran = poll_once(claimer, run, dispatcher.lease, wid)
        polls += 1
        if polls % dispatcher.sweep_every == 0:
            sweep(claimer, spool, dispatcher.spool_max_age)
        if ran is None:
            time.sleep(dispatcher.interval)
