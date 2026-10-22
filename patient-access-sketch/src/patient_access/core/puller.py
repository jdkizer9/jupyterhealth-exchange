"""Phase 4: walk the vendor's query table and spool every page. Filled in at M3."""

from __future__ import annotations

from collections.abc import Callable

from .interfaces import SpoolHandle
from .models import Job
from .pipeline import FetchAborted
from .vendors import VendorConfig


def fetch(job: Job, spool: SpoolHandle, vendor: VendorConfig, save: Callable[[], None]) -> None:
    raise FetchAborted("fetch is not implemented yet")
