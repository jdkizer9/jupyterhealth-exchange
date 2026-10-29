"""The plugs (design-sketch.md §7). Core depends on these; implementations live one layer out."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Protocol, runtime_checkable

from .models import (
    AuthAttempt,
    BlobMeta,
    ContentPolicy,
    Endpoint,
    ImportCounts,
    InboundRequest,
    Job,
    JobId,
    Participant,
    Receipt,
    Selection,
    SpoolIndex,
    Unit,
    UnitOutcome,
)
from .source_settings import SourceSettings


class IdentityError(Exception):
    """The inbound request does not identify a usable participant. The message is safe to show."""


@runtime_checkable
class IdentityProvider(Protocol):
    name: str

    def resolve(self, request: InboundRequest) -> Participant:
        """Phase 1, on open. Raises IdentityError."""
        ...

    def commit(self, participant: Participant) -> None:
        """End of phase 3, right before the Job row is created. Spends the identity if the provider
        has a single-use rule. Raises IdentityError if it can no longer be used."""
        ...


class Directory(Protocol):
    """Who the patient can connect to, and how (design §10.8). Shaped like the SMART User-access
    Brands format: facilities under brands, brands on endpoints, endpoints under a customer and a
    vendor. Vendor settings are part of it: `settings_for` is the one place the layering of
    registration, profile and overrides is visible."""

    def search(
        self, q: str, state: str | None = None, postal: str | None = None, limit: int = 25
    ) -> list[Selection]: ...

    def selection(self, selection_id: str) -> Selection:
        """Raises KeyError."""
        ...

    def settings_for(self, endpoint: Endpoint) -> SourceSettings:
        """Registration and fetch profile for an endpoint, overrides merged. Raises KeyError when
        the endpoint's vendor has no registration."""
        ...

    def upsert_brands_bundle(self, bundle: dict, vendor: str) -> ImportCounts: ...


class AuthAttemptStore(Protocol):
    def create(self, attempt: AuthAttempt) -> str: ...

    def consume(self, state: str) -> AuthAttempt:
        """Single use. Raises KeyError if unknown, expired or already used."""
        ...


class JobStore(Protocol):
    def create(self, job: Job) -> JobId: ...

    def get(self, job_id: JobId) -> Job:
        """Raises KeyError."""
        ...

    def save(self, job: Job) -> None: ...


class SpoolHandle(Protocol):
    ref: str

    def append(self, resource_type: str, resource: dict) -> int:
        """Phase 4. Returns bytes written."""
        ...

    def has_resource(self, resource_type: str, resource_id: str) -> bool: ...

    def flush(self, resource_type: str) -> None:
        """Make everything appended for this type durable. The puller calls it before recording the
        type as done, so a crash can never leave an outcome that says "ok" over data that was lost."""
        ...

    def reset_type(self, resource_type: str) -> None:
        """Drop what a previous, unfinished attempt wrote for this type (resume)."""
        ...

    def put_blob(self, blob_id: str, data: bytes, meta: BlobMeta) -> None: ...

    def iter_type(self, resource_type: str) -> Iterator[dict]:
        """Phase 5."""
        ...

    def get_blob(self, blob_id: str) -> bytes: ...

    def index(self) -> SpoolIndex: ...

    def close(self) -> None: ...


class Spool(Protocol):
    def open(self, job: Job, data_key: bytes) -> SpoolHandle:
        """Create or reopen the job's spool. Every implementation encrypts with data_key."""
        ...

    def delete(self, spool_ref: str) -> None: ...

    def sweep(self, keep: set[str], older_than: timedelta) -> list[str]:
        """Delete spools not in `keep` and older than `older_than`. Returns what was deleted."""
        ...


class Formatter(Protocol):
    produces: frozenset[str]

    def format(self, spool: SpoolHandle, job: Job, content: ContentPolicy) -> Iterator[Unit]:
        """Units for one destination, honouring its content policy (types and documents)."""
        ...


class DestinationResolver(Protocol):
    def resolve(self, job: Job) -> tuple[str, ...]:
        """Which configured destinations this job delivers to, asked at delivery time so a
        consent added while the job was queued is picked up. The result is unioned with what the
        launch recorded on the job."""
        ...


class SinkSession(Protocol):
    def write(self, unit: Unit, spool: SpoolHandle) -> UnitOutcome:
        """`spool` is passed so the sink can stream a unit's blobs without the formatter loading them."""
        ...

    def finish(self) -> Receipt: ...

    def abort(self, reason: str) -> None: ...


class Sink(Protocol):
    accepts: frozenset[str]

    def begin(self, job: Job) -> SinkSession: ...


class JobDispatcher(Protocol):
    def dispatch(self, job_id: JobId) -> None:
        """Web side, right after JobStore.create. The worker side is host-run and calls run_job."""
        ...
