"""DramatiqDispatcher: the same JobDispatcher seam, backed by a Redis broker (plan §5 stretch).

Web side: `dispatch(job_id)` sends a message carrying only the id. Worker side: the host runs
    dramatiq patient_access.django_apps.jobs.dramatiq_worker
which builds the module and registers `run` here, so the actor calls run_job without this module
importing Django. The Job row stays the system of record (design §7.2): the message is only a
nudge, and a lost one is recovered by re-dispatching queued jobs, never by replaying the queue.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from patient_access.core.interfaces import JobDispatcher
from patient_access.core.models import JobId

log = logging.getLogger(__name__)

QUEUE = "patient_access"

# The broker must exist before the actor is declared, so it is configured at import from the env.
dramatiq.set_broker(RedisBroker(url=os.environ.get("PA_DRAMATIQ_REDIS_URL", "redis://localhost:6379/0")))

_runner: Callable[[JobId], Any] | None = None


def set_runner(run: Callable[[JobId], Any]) -> None:
    global _runner
    _runner = run


@dramatiq.actor(queue_name=QUEUE, max_retries=0, time_limit=60 * 60 * 1000)
def run_job_actor(job_id: str) -> None:
    # No broker retries: run_job records failures on the row and is resumable; retrying is the
    # row's business (status `failed`), not the broker's.
    if _runner is None:
        raise RuntimeError("no runner registered; start the worker via patient_access.django_apps.jobs.dramatiq_worker")
    _runner(job_id)


class DramatiqDispatcher(JobDispatcher):
    def __init__(self):
        self.actor = run_job_actor

    def dispatch(self, job_id: JobId) -> None:
        self.actor.send(job_id)
        log.info("job %s sent to dramatiq queue %s", job_id, QUEUE)
