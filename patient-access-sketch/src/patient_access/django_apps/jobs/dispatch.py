"""Dispatching from inside a transaction."""

from __future__ import annotations

from django.db import transaction

from patient_access.core.interfaces import JobDispatcher


class OnCommitDispatcher(JobDispatcher):
    """Hand the job to the real dispatcher only once the surrounding transaction commits.

    A callback creates the job row and dispatches in one transaction; a thread or broker worker
    started before the commit would look for a row it cannot see yet, fail to claim it, and
    leave the job to the sweep. Outside a transaction, on_commit runs at once.
    """

    def __init__(self, inner: JobDispatcher):
        self.inner = inner

    def dispatch(self, job_id: str) -> None:
        transaction.on_commit(lambda: self.inner.dispatch(job_id))
