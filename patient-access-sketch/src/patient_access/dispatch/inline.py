"""InlineDispatcher: runs the job in the caller's process, synchronously. Tests and demos only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from patient_access.core.models import JobId


class InlineDispatcher:
    def __init__(self, run: Callable[[JobId], Any]):
        self.run = run

    def dispatch(self, job_id: JobId) -> None:
        self.run(job_id)
