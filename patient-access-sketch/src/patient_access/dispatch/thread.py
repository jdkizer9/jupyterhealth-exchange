"""ThreadDispatcher: run the job in a daemon thread of the web process, right after dispatch.

The no-new-infrastructure runner for a host like JHE (sync gunicorn, no broker): the callback
view redirects at once and the pull proceeds in the background of the same process. It claims
the job through the same lease the polling worker uses, so a cron tick running
`run_jobs --once` (the sweep) can never run the same job concurrently, and will pick up anything a
thread dropped (deploy, worker recycle, machine stop) once the lease expires. Core is untouched:
this is `JobDispatcher.dispatch` plus the host's existing `run`.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol

from patient_access.core.interfaces import JobDispatcher
from patient_access.core.models import JobId

log = logging.getLogger(__name__)


class ClaimById(Protocol):
    def claim(self, job_id: JobId, worker_id: str, lease: timedelta) -> bool: ...

    def release(self, job_id: JobId, worker_id: str) -> None: ...


class ThreadDispatcher(JobDispatcher):
    def __init__(
        self,
        run: Callable[[JobId], Any],
        claimer: ClaimById,
        lease_seconds: int = 600,
        on_thread_exit: Callable[[], None] = lambda: None,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ):
        """on_thread_exit: host cleanup for the thread, e.g. closing Django's per-thread DB connection."""
        self.run = run
        self.claimer = claimer
        self.lease = timedelta(seconds=lease_seconds)
        self.on_thread_exit = on_thread_exit
        self.thread_factory = thread_factory
        self._active: dict[JobId, str] = {}
        self._lock = threading.Lock()
        # Daemon threads are killed when the process exits (a deploy); releasing their leases
        # first lets the sweep resume those jobs on its next tick instead of after the lease expires.
        atexit.register(self.release_all)

    def release_all(self) -> None:
        with self._lock:
            active = dict(self._active)
            self._active.clear()
        for job_id, wid in active.items():
            try:
                self.claimer.release(job_id, wid)
            except Exception:  # noqa: BLE001 - best effort at shutdown
                log.exception("job %s: lease release at exit failed", job_id)

    def dispatch(self, job_id: JobId) -> None:
        t = self.thread_factory(target=self._work, args=(job_id,), name=f"pa-job-{job_id[:8]}", daemon=True)
        t.start()

    def _work(self, job_id: JobId) -> None:
        wid = f"thread:{threading.get_ident()}"
        try:
            if not self.claimer.claim(job_id, wid, self.lease):
                log.info("job %s: not claimed by thread (already leased or terminal)", job_id)
                return
            with self._lock:
                self._active[job_id] = wid
            try:
                self.run(job_id)
            except Exception:  # noqa: BLE001 - run_job records failures; the sweep retries
                log.exception("job %s: thread run failed", job_id)
            finally:
                with self._lock:
                    self._active.pop(job_id, None)
                self.claimer.release(job_id, wid)
        finally:
            self.on_thread_exit()
