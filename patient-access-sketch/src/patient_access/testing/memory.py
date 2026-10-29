"""In-memory implementation of every Protocol, for core tests without a database (design §9 rule 3)."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta

from patient_access.core.interfaces import (
    AuthAttemptStore,
    Directory,
    Formatter,
    IdentityError,
    IdentityProvider,
    JobStore,
    Sink,
    SinkSession,
    Spool,
    SpoolHandle,
)
from patient_access.core.models import (
    AuthAttempt,
    BlobMeta,
    Brand,
    Endpoint,
    Facility,
    ImportCounts,
    InboundRequest,
    Job,
    JobId,
    Participant,
    Receipt,
    Selection,
    Source,
    SpoolIndex,
    Unit,
    UnitOutcome,
)
from patient_access.core.source_settings import (
    ClientRegistration,
    FetchProfile,
    QuerySpec,
    SourceSettings,
    apply_overrides,
)


class StaticIdentity(IdentityProvider):
    """Resolves every request to the same participant (or to `?ref=`)."""

    name = "static"

    def __init__(self, ref: str = "test-participant"):
        self.ref = ref
        self.committed: list[str] = []

    def resolve(self, request: InboundRequest) -> Participant:
        ref = request.query.get("ref", self.ref)
        if not ref:
            raise IdentityError("no participant")
        return Participant(ref=ref, provider=self.name)

    def commit(self, participant: Participant) -> None:
        self.committed.append(participant.ref)


class InMemoryDirectory(Directory):
    """Selections and vendor settings given directly; `overrides` narrows a profile per customer id."""

    def __init__(
        self,
        selections: list[Selection] | None = None,
        settings: dict[str, SourceSettings] | None = None,
        overrides: dict[str, dict] | None = None,
    ):
        self.selections = {s.id: s for s in selections or []}
        self.settings = dict(settings or {})
        self.overrides = dict(overrides or {})

    def search(self, q: str, state: str | None = None, postal: str | None = None, limit: int = 25) -> list[Selection]:
        q = (q or "").lower()
        out = [
            s
            for s in self.selections.values()
            if (
                not q
                or q in s.name.lower()
                or q in s.brand.name.lower()
                or q in ((s.facility and s.facility.city) or "").lower()
            )
            and (not state or (s.facility and s.facility.state == state))
            and (not postal or ((s.facility and s.facility.postal_code) or "").startswith(postal))
        ]
        return sorted(out, key=lambda s: s.name)[:limit]

    def selection(self, selection_id: str) -> Selection:
        return self.selections[selection_id]

    def settings_for(self, endpoint: Endpoint) -> SourceSettings:
        base = self.settings[endpoint.vendor]
        override = self.overrides.get(endpoint.customer_id or "")
        return replace(base, fetch=apply_overrides(base.fetch, override)) if override else base

    def upsert_brands_bundle(self, bundle: dict, vendor: str) -> ImportCounts:
        raise NotImplementedError("the in-memory directory is seeded directly")


def selection(
    sid: str = "sel-1",
    *,
    vendor: str = "v",
    iss: str = "https://ehr.example/fhir",
    brand: str = "Brand",
    facility: str | None = "Clinic",
    customer_id: str | None = None,
) -> Selection:
    """A test Selection: brand on an endpoint, at a facility unless facility=None."""
    ep = Endpoint(id=f"{sid}-ep", iss=iss, vendor=vendor, customer_id=customer_id)
    return Selection(
        id=sid,
        brand=Brand(id=f"{sid}-b", name=brand, endpoint=ep),
        facility=Facility(
            id=f"{sid}-f", name=facility, address="1 Main St", city="Boston", state="MA", postal_code="02115"
        )
        if facility
        else None,
    )


def settings(vendor: str = "v", queries: tuple[QuerySpec, ...] = (), **kw) -> SourceSettings:
    """Test SourceSettings: a public registration and a profile of `queries`; `kw` go to FetchProfile
    (page_cap, documents, ...) or, for auth_method / client_id / scope_version / base_scopes, to the registration."""
    reg_keys = {
        "client_id",
        "auth_method",
        "scope_version",
        "base_scopes",
        "client_secret",
        "private_key_pem",
        "key_id",
        "jwt_alg",
    }
    reg = {k: kw.pop(k) for k in list(kw) if k in reg_keys}
    return SourceSettings(
        registration=ClientRegistration(vendor=vendor, client_id=reg.pop("client_id", "c"), **reg),
        fetch=FetchProfile(name="test", queries=queries, **kw),
    )


def source(sel: Selection | None = None, **kw) -> Source:
    """A test Source from a Selection (default `selection()`), with empty scopes and patient "pat-1"."""
    args = {"scopes_requested": [], "scopes_granted": [], "ehr_patient_id": "pat-1", **kw}
    return Source.from_selection(sel or selection(), **args)


class InMemoryAuthAttemptStore(AuthAttemptStore):
    def __init__(self, clock=None):
        self.attempts: dict[str, AuthAttempt] = {}
        self.clock = clock

    def create(self, attempt: AuthAttempt) -> str:
        self.attempts[attempt.state] = copy.deepcopy(attempt)
        return attempt.state

    def consume(self, state: str) -> AuthAttempt:
        attempt = self.attempts.get(state)
        now = self.clock() if self.clock else datetime.now(tz=attempt.expires_at.tzinfo if attempt else None)
        if attempt is None or attempt.used_at is not None or attempt.expires_at <= now:
            raise KeyError(state)
        attempt.used_at = now
        return copy.deepcopy(attempt)


class InMemoryJobStore(JobStore):
    """Deep-copies on the way in and out, so tests catch code that forgets to save."""

    def __init__(self):
        self.jobs: dict[JobId, Job] = {}
        self.saves = 0

    def create(self, job: Job) -> JobId:
        if not job.id:
            job.id = uuid.uuid4().hex
        self.jobs[job.id] = copy.deepcopy(job)
        return job.id

    def get(self, job_id: JobId) -> Job:
        return copy.deepcopy(self.jobs[job_id])

    def save(self, job: Job) -> None:
        self.saves += 1
        self.jobs[job.id] = copy.deepcopy(job)


class InMemorySpoolHandle(SpoolHandle):
    def __init__(self, ref: str, store: dict):
        self.ref = ref
        self._s = store

    def append(self, resource_type: str, resource: dict) -> int:
        line = json.dumps(resource)
        self._s["types"].setdefault(resource_type, []).append(line)
        self._s["bytes"] += len(line)
        return len(line)

    def flush(self, resource_type: str) -> None:
        pass

    def has_resource(self, resource_type: str, resource_id: str) -> bool:
        return any(json.loads(line).get("id") == resource_id for line in self._s["types"].get(resource_type, []))

    def reset_type(self, resource_type: str) -> None:
        self._s["types"].pop(resource_type, None)

    def put_blob(self, blob_id: str, data: bytes, meta: BlobMeta) -> None:
        self._s["blobs"][blob_id] = (data, meta)
        self._s["bytes"] += len(data)

    def iter_type(self, resource_type: str) -> Iterator[dict]:
        for line in self._s["types"].get(resource_type, []):
            yield json.loads(line)

    def get_blob(self, blob_id: str) -> bytes:
        return self._s["blobs"][blob_id][0]

    def index(self) -> SpoolIndex:
        return SpoolIndex(
            types={t: len(v) for t, v in self._s["types"].items()},
            blobs={k: m for k, (_, m) in self._s["blobs"].items()},
            bytes=self._s["bytes"],
        )

    def close(self) -> None:
        pass


class InMemorySpool(Spool):
    def __init__(self):
        self.spools: dict[str, dict] = {}
        self.deleted: list[str] = []

    def open(self, job: Job, data_key: bytes) -> InMemorySpoolHandle:
        ref = job.spool_ref or f"mem-{job.id}"
        store = self.spools.setdefault(ref, {"types": {}, "blobs": {}, "bytes": 0, "created": datetime.now()})
        return InMemorySpoolHandle(ref, store)

    def delete(self, spool_ref: str) -> None:
        self.spools.pop(spool_ref, None)
        self.deleted.append(spool_ref)

    def sweep(self, keep: set[str], older_than: timedelta) -> list[str]:
        cutoff = datetime.now() - older_than
        gone = [ref for ref, s in self.spools.items() if ref not in keep and s["created"] < cutoff]
        for ref in gone:
            self.delete(ref)
        return gone


class PassthroughFormatter(Formatter):
    """One `collection` Bundle per resource type, resources as fetched."""

    produces = frozenset({"collection"})

    def format(self, spool, job: Job) -> Iterator[Unit]:
        for resource_type, count in sorted(spool.index().types.items()):
            entries = [{"resource": r} for r in spool.iter_type(resource_type)]
            yield Unit(
                name=resource_type,
                kind="collection",
                body={"resourceType": "Bundle", "type": "collection", "entry": entries},
                resources=count,
            )


class MemorySink(Sink):
    accepts = frozenset({"collection", "batch", "document", "metadata"})

    def __init__(self, fail_on: str | None = None):
        self.delivered: dict[JobId, list[Unit]] = {}
        self.aborted: list[tuple[JobId, str]] = []
        self.fail_on = fail_on  # unit name that raises, to test retries

    def begin(self, job: Job):
        sink = self

        class Session(SinkSession):
            def __init__(self):
                self.units: list[Unit] = []

            def write(self, unit: Unit, spool: SpoolHandle) -> UnitOutcome:
                if sink.fail_on and unit.name == sink.fail_on:
                    raise RuntimeError(f"configured to fail on {unit.name}")
                self.units.append(unit)
                return UnitOutcome(ok=unit.resources)

            def finish(self) -> Receipt:
                sink.delivered[job.id] = self.units
                return Receipt(
                    destination="memory",
                    location=f"memory:{job.id}",
                    units=len(self.units),
                    resources=sum(u.resources for u in self.units),
                )

            def abort(self, reason: str) -> None:
                sink.aborted.append((job.id, reason))

        return Session()
