"""Phase 4, second pass: document content referenced by the fetched resources. Filled in at M5."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .interfaces import SpoolHandle
from .models import Job
from .vendors import VendorConfig

if TYPE_CHECKING:
    from .puller import FhirClient


def fetch_documents(
    job: Job, spool: SpoolHandle, vendor: VendorConfig, client: FhirClient, save: Callable[[], None]
) -> None:
    job.documents.status = "skipped"
    save()
