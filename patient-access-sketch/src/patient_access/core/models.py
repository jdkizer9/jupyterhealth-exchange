"""Dataclasses core speaks. No ORM instances cross into core; django_app translates at the edge."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

JobId = str


class Secrets:
    """String secrets that must never be logged. Encrypted at rest by whichever store persists them."""

    def __init__(self, values: Mapping[str, str] | None = None):
        self._values: dict[str, str] = dict(values or {})

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._values[key] = value

    def pop(self, key: str, default: str | None = None) -> str | None:
        return self._values.pop(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._values

    def __bool__(self) -> bool:
        return bool(self._values)

    def clear(self) -> None:
        self._values.clear()

    def reveal(self) -> dict[str, str]:
        """The plain values, for a store that is about to encrypt them."""
        return dict(self._values)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secrets) and other._values == self._values

    def __repr__(self) -> str:
        return f"Secrets(keys={sorted(self._values)})"


# --- phase 1: identity ------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundRequest:
    """What an IdentityProvider may look at. Built by the web layer from its framework's request."""

    query: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased names


@dataclass
class Participant:
    """Who a pull is for. `ref` is opaque to core; only the sink interprets it."""

    ref: str
    provider: str  # name of the IdentityProvider that resolved it
    attrs: dict[str, Any] = field(default_factory=dict)
    secrets: Secrets = field(default_factory=Secrets)
    # Provider-private handle that `commit` needs, e.g. the deep link's jti. Not a secret.
    handle: str = ""


# --- phase 2: directory -----------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    """A row the patient can pick: a care facility under a brand, which carries the SMART iss."""

    id: str
    name: str
    address: str
    city: str | None
    state: str | None
    postal_code: str | None
    brand_id: str
    brand_name: str
    iss: str
    vendor: str


@dataclass(frozen=True)
class ImportCounts:
    brands: int
    locations: int


# --- phase 3: upstream OAuth ------------------------------------------------------------------


@dataclass
class AuthAttempt:
    """Server-side state across the EHR redirect. Replaces the browser's sessionStorage keys."""

    state: str
    code_verifier: str
    participant: Participant
    location_id: str
    vendor: str
    iss: str
    scopes: list[str]
    redirect_uri: str
    token_endpoint: str
    destination: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None


@dataclass
class TokenResponse:
    access_token: str
    expires_at: datetime
    scopes_granted: list[str]
    patient: str
    token_type: str = "Bearer"
    raw: dict[str, Any] = field(default_factory=dict)  # minus the tokens


# --- the job ----------------------------------------------------------------------------------


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    FETCHED = "fetched"
    DELIVERING = "delivering"  # format and sink interleave: the formatter is a generator the sink drains
    DONE = "done"
    FAILED = "failed"  # retryable from the spool (format/sink failed)
    FAILED_TERMINAL = "failed_terminal"  # nothing more can happen; spool deleted

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.DONE, JobStatus.FAILED_TERMINAL)


@dataclass
class Source:
    """Where the data comes from, fixed at the end of phase 3."""

    vendor: str
    iss: str
    brand_name: str
    location_id: str
    location_name: str
    scopes_requested: list[str]
    scopes_granted: list[str]
    ehr_patient_id: str


@dataclass
class TypeOutcome:
    """Phase 4 result for one row of the vendor's query table."""

    label: str
    resource_type: str
    query: str
    status: str = "pending"  # pending | ok | error | skipped
    count: int = 0
    pages: int = 0
    bytes: int = 0
    error: str | None = None
    capped: bool = False


@dataclass
class DocumentOutcome:
    """Phase 4 documents pass totals."""

    status: str = "pending"  # pending | ok | skipped | error
    candidates: int = 0
    fetched: int = 0
    inline: int = 0
    bytes: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count (caps, errors)
    error: str | None = None


@dataclass
class Receipt:
    destination: str
    location: str  # where it landed: a prefix, a directory, a FhirSource id
    units: int = 0
    resources: int = 0
    blobs: int = 0
    bytes: int = 0
    failures: dict[str, int] = field(default_factory=dict)  # reason -> count
    warnings: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Job:
    id: JobId
    participant: Participant
    source: Source
    destination: str  # name of a configured Destination
    status: JobStatus = JobStatus.QUEUED
    # EHR access token and the spool data key. Encrypted at rest by the JobStore; cleared at a
    # terminal state (and the EHR token as soon as the fetch ends).
    credentials: Secrets = field(default_factory=Secrets)
    token_expires_at: datetime | None = None
    outcomes: list[TypeOutcome] = field(default_factory=list)
    documents: DocumentOutcome = field(default_factory=DocumentOutcome)
    spool_ref: str | None = None
    receipt: Receipt | None = None
    failure: str | None = None
    attempts: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    def outcome(self, label: str) -> TypeOutcome | None:
        return next((o for o in self.outcomes if o.label == label), None)


# --- spool, formatter, sink -------------------------------------------------------------------


@dataclass(frozen=True)
class BlobMeta:
    content_type: str
    size: int
    sha256: str
    source_url: str | None = None  # where it came from at the EHR
    referenced_by: str | None = None  # e.g. "DocumentReference/123"


@dataclass
class SpoolIndex:
    types: dict[str, int] = field(default_factory=dict)  # resource type -> count
    blobs: dict[str, BlobMeta] = field(default_factory=dict)  # blob id -> meta
    bytes: int = 0


@dataclass(frozen=True)
class BlobRef:
    """A sidecar blob a unit refers to; the sink stores it at `key` relative to its own root."""

    blob_id: str
    key: str
    meta: BlobMeta


@dataclass
class Unit:
    """One thing the formatter hands the sink: a FHIR Bundle (kind = Bundle.type) or a companion
    JSON document (kind = "metadata"), plus any blobs that travel beside it."""

    name: str
    kind: str
    body: dict[str, Any]
    blobs: list[BlobRef] = field(default_factory=list)
    resources: int = 0  # how many resources it carries, for the receipt


@dataclass
class UnitOutcome:
    ok: int = 0
    failed: int = 0
    bytes: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
