"""The plugs (design-sketch.md §7). Core depends on these; implementations live one layer out.

A pull runs in five phases, and each plug belongs to one or two of them:

    1. open      the participant arrives (deep link, JHE session)      IdentityProvider
    2. pick      they choose where to connect                          Directory
    3. authorize SMART launch, callback, token exchange                Directory, AuthAttemptStore,
                                                                       IdentityProvider.commit,
                                                                       JobStore, JobDispatcher
    4. fetch     the worker pulls every granted type into the spool    Spool, SpoolHandle
    5. deliver   formatters turn the spool into units, sinks take them Formatter, Sink, SinkSession

Phases 1 to 3 run in the web process (`flow.py`); 4 and 5 run in a worker (`pipeline.run_job`).
Every plug is a Protocol: an implementation needs the methods, not the base class. The in-memory
versions in `testing/memory.py` are the reference behaviour.
"""

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
    """Who is pulling, and on whose behalf the data lands (phase 1, and the end of phase 3).

    The host decides what an identity is: a signed deep link minted by a study, a JHE session
    with a logged-in patient, a fixed participant in tests. Core only needs a `Participant` out of
    it. The participant's `attrs` travel with the job and are what sinks use to address the data
    (JHE: patient_id, fhir_source_id); its `secrets` are encrypted at rest and cleared when the
    job ends.
    """

    name: str
    """Recorded on the participant as `provider`, so a job says how its identity was established."""

    def resolve(self, request: InboundRequest) -> Participant:
        """Phase 1, on open: turn the inbound request into a participant.

        Must not spend anything: the same link may be opened twice before a pull is started.
        Raises IdentityError with a message the browser may show.
        """
        ...

    def commit(self, participant: Participant) -> None:
        """End of phase 3, right before the Job row is created, after the EHR token exchange
        succeeded. Spends the identity if the provider has a single-use rule (a deep link), and
        raises IdentityError if it can no longer be used. A provider with nothing to spend does
        nothing.
        """
        ...


class Directory(Protocol):
    """Who the patient can connect to, and how (design §10.8). Shaped like the SMART User-access
    Brands format: facilities under brands, brands on endpoints, endpoints under a customer and a
    vendor. Vendor settings are part of it: `settings_for` is the one place the layering of
    registration, profile and overrides is visible.

    Phase 2 uses `search` and `selection`; phase 3 and the worker use `settings_for`. The
    standalone host keeps the directory in its own tables (`django_apps/directory`); JHE maps it
    over the EhrVendor / EhrBrand / EhrBrandLocation rows it already has.
    """

    def search(self, q: str, state: str | None = None, postal: str | None = None, limit: int = 25) -> list[Selection]:
        """Phase 2, the picker: selections whose brand, facility or city match the words in `q`,
        narrowed by state and postal code prefix when given. Ordering is the implementation's."""
        ...

    def selection(self, selection_id: str) -> Selection:
        """The selection an id from `search` (or a deep link's `brand` attr) refers to. The
        selection is stored on the auth attempt as a value, so a later re-import cannot strand a
        pull in progress. Raises KeyError when the id is unknown or no longer valid."""
        ...

    def settings_for(self, endpoint: Endpoint) -> SourceSettings:
        """Registration and fetch profile for an endpoint, overrides merged (vendor, then customer,
        then endpoint). Called at launch, again at the callback and once per worker run, never
        stored: a rotated credential or a tightened profile takes effect on the next call. Raises
        KeyError when the endpoint's vendor has no registration, which the flow reports as "not
        available for connection yet"."""
        ...

    def upsert_brands_bundle(self, bundle: dict, vendor: str) -> ImportCounts:
        """Load or refresh a vendor's User-access Brands bundle (Organization and Endpoint
        resources) under the named vendor. Idempotent: re-importing the same bundle changes
        nothing. Used by seeding and scheduled refreshes, not by the flow."""
        ...


class AuthAttemptStore(Protocol):
    """Phase 3's pending authorizations, keyed by the OAuth `state`.

    An attempt lives from the redirect to the EHR until the callback, a few minutes at most. It
    carries the PKCE verifier and the participant's secrets, so implementations encrypt it at rest.
    """

    def create(self, attempt: AuthAttempt) -> str:
        """Record the attempt before the browser is redirected. Returns its id (the `state`)."""
        ...

    def consume(self, state: str) -> AuthAttempt:
        """The callback's single read. Marks the attempt used so a replayed callback cannot start
        a second job. Raises KeyError if the state is unknown, expired or already used."""
        ...


class JobStore(Protocol):
    """Where jobs live between the callback and the end of delivery.

    `Job` is the whole record: status, per-type outcomes, deliveries, spool ref and the EHR
    credentials (encrypted at rest, cleared on finish). The pipeline saves after every state
    change so a worker that dies mid-run leaves an honest row for the next one to resume from.
    """

    def create(self, job: Job) -> JobId:
        """Persist a new job (status queued) and return the id the dispatcher will be handed."""
        ...

    def get(self, job_id: JobId) -> Job:
        """Load a job. Raises KeyError."""
        ...

    def save(self, job: Job) -> None:
        """Write the job back. Called often; implementations should write the whole record, not
        diff it, so a resumed worker sees exactly what the last one saw."""
        ...


class SpoolHandle(Protocol):
    """One job's spool, open: where phase 4 writes resources and phase 5 reads them back.

    Resources are appended per type in arrival order and read back in the same order. Blobs are
    the attachments the documents pass downloads, stored beside the resources and referenced from
    units by id. Everything is encrypted with the job's data key, so the spool's own storage
    never holds plaintext PHI.
    """

    ref: str
    """Opaque locator stored on the job (`spool_ref`); `Spool.open` reopens by it, `delete`
    and `sweep` take it."""

    def append(self, resource_type: str, resource: dict) -> int:
        """Phase 4: add one resource under its type. Returns the bytes written, for the outcome.
        Not durable until `flush`."""
        ...

    def has_resource(self, resource_type: str, resource_id: str) -> bool:
        """Whether a resource of that type and id is already spooled. A hook for de-duplicating
        a resumed fetch; core's puller does not call it today (it resets the type instead)."""
        ...

    def flush(self, resource_type: str) -> None:
        """Make everything appended for this type durable. The puller calls it before recording the
        type as done, so a crash can never leave an outcome that says "ok" over data that was lost."""
        ...

    def reset_type(self, resource_type: str) -> None:
        """Drop what a previous, unfinished attempt wrote for this type (resume). The puller then
        refetches the type from its first page."""
        ...

    def put_blob(self, blob_id: str, data: bytes, meta: BlobMeta) -> None:
        """Phase 4, documents pass: store one downloaded attachment with its content type, size
        and provenance. Durable on return."""
        ...

    def iter_type(self, resource_type: str) -> Iterator[dict]:
        """Phase 5: the resources of one type, in the order they were appended. Yields nothing for
        a type that was never written."""
        ...

    def get_blob(self, blob_id: str) -> bytes:
        """Phase 5: one attachment's bytes, for a sink that writes blobs itself (S3, local dir).
        Raises KeyError for an unknown id."""
        ...

    def index(self) -> SpoolIndex:
        """What the spool holds: a count per type, every blob's metadata, total bytes. The
        pipeline compares it with the job's outcomes on resume to detect a lost spool."""
        ...

    def close(self) -> None:
        """Release file handles or buffers. The pipeline calls it in a `finally`; the data stays."""
        ...


class Spool(Protocol):
    """The spool store: creates, reopens and deletes per-job spools.

    Local disk in the simplest host, S3 staging where workers do not share a disk. The data key
    comes from the job's encrypted credentials, so deleting the job's credentials crypto-shreds
    the spool even if its bytes linger.
    """

    def open(self, job: Job, data_key: bytes) -> SpoolHandle:
        """Create the job's spool, or reopen it by `job.spool_ref` on a resumed run. Every
        implementation encrypts with `data_key` (32 bytes, generated by core)."""
        ...

    def delete(self, spool_ref: str) -> None:
        """Remove a spool and everything in it. Called when a job reaches a terminal state.
        Must tolerate a spool that is already gone."""
        ...

    def sweep(self, keep: set[str], older_than: timedelta) -> list[str]:
        """Garbage collection for spools whose job never finished (a worker that died before
        `delete`). Delete spools not in `keep` and older than `older_than`. Returns what was
        deleted. The dispatcher's maintenance pass calls it with the refs of live jobs."""
        ...


class Formatter(Protocol):
    """Phase 5: turns the spool into the units a destination wants.

    A formatter decides the shape (one R4 batch Bundle per type, a single Composition document
    with its metadata, ...); the sink decides where it goes. `Destination` checks at construction
    that the sink accepts every kind the formatter produces.
    """

    produces: frozenset[str]
    """Unit kinds this formatter yields: a Bundle type ("batch", "collection", "document") or
    "metadata". Checked against `Sink.accepts`."""

    def format(self, spool: SpoolHandle, job: Job, content: ContentPolicy) -> Iterator[Unit]:
        """Units for one destination, honouring its content policy (types and documents). Yield
        lazily: a unit may be large, and the sink writes each before the next is built. Blobs are
        referenced, not loaded; the sink fetches them from the spool if it stores them."""
        ...


class SinkSession(Protocol):
    """One delivery to one destination, from `Sink.begin` to `finish` or `abort`.

    Sessions make a delivery atomic from the consumer's point of view: nothing a consumer sees
    should say "complete" until `finish` has run, and `abort` cleans up what a failed attempt
    wrote where the store allows it (S3 deletes its keys; JHE cannot un-import and relies on its
    duplicate rejection instead) so a retry starts clean.
    """

    def write(self, unit: Unit, spool: SpoolHandle) -> UnitOutcome:
        """Take one unit. `spool` is passed so the sink can stream a unit's blobs without the
        formatter loading them. Raise on any error that should fail the delivery; the pipeline
        aborts the session and retries later (the spool still has the data)."""
        ...

    def finish(self) -> Receipt:
        """Seal the delivery and say what landed where: the location (a prefix, a directory, a
        FhirSource id), counts, and any per-unit failures the sink chose to tolerate."""
        ...

    def abort(self, reason: str) -> None:
        """Undo a partial delivery after `write` or `finish` raised. Best effort; the pipeline
        logs a failure here and still retries the delivery."""
        ...


class Sink(Protocol):
    """Phase 5: a place data is delivered to (S3, a local directory, JHE's own tables).

    A sink is configured once and used for every job; per-job state lives in the session.
    """

    accepts: frozenset[str]
    """Unit kinds this sink can store. Checked against `Formatter.produces` when a Destination
    is built, so a mismatch is a configuration error, not a delivery failure."""

    def begin(self, job: Job) -> SinkSession:
        """Start a delivery for this job. Anything the sink registers up front (JHE: the
        patient identifier and FhirSource) happens here, inside the delivery's try, so it fails
        and retries like any other delivery error."""
        ...


class JobDispatcher(Protocol):
    """Hands a newly created job to whatever runs the worker side.

    Threads in the same process, a DB-polling loop, a Dramatiq queue, or on-commit scheduling.
    The worker side is host-run and calls `pipeline.run_job`; the dispatcher only wakes it.
    """

    def dispatch(self, job_id: JobId) -> None:
        """Web side, right after JobStore.create. Must return quickly; only `InlineDispatcher`
        (dev and tests) runs the job in the caller. A lost dispatch is recoverable: the row's
        `queued` status is the real queue, and a polling worker or the cron sweep claims it."""
        ...
