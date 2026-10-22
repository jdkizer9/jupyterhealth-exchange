"""In-memory implementation of every Protocol, for core tests without a database (design §9 rule 3)."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta

from patient_access.core.interfaces import IdentityError
from patient_access.core.models import (
    AuthAttempt,
    BlobMeta,
    ImportCounts,
    InboundRequest,
    Job,
    JobId,
    Location,
    Participant,
    Receipt,
    SpoolIndex,
    Unit,
    UnitOutcome,
)


class StaticIdentity:
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


class InMemoryDirectory:
    def __init__(self, locations: list[Location] | None = None):
        self.locations = {loc.id: loc for loc in locations or []}

    def search(self, q: str, state: str | None = None, postal: str | None = None, limit: int = 25) -> list[Location]:
        q = (q or "").lower()
        out = [
            loc
            for loc in self.locations.values()
            if (not q or q in loc.name.lower() or q in loc.brand_name.lower() or q in (loc.city or "").lower())
            and (not state or loc.state == state)
            and (not postal or (loc.postal_code or "").startswith(postal))
        ]
        return sorted(out, key=lambda loc: loc.name)[:limit]

    def location(self, location_id: str) -> Location:
        return self.locations[location_id]

    def upsert_brands_bundle(self, bundle: dict, vendor: str) -> ImportCounts:
        raise NotImplementedError("the in-memory directory is seeded directly")


class InMemoryAuthAttemptStore:
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


class InMemoryJobStore:
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


class InMemorySpoolHandle:
    def __init__(self, ref: str, store: dict):
        self.ref = ref
        self._s = store

    def append(self, resource_type: str, resource: dict) -> int:
        line = json.dumps(resource)
        self._s["types"].setdefault(resource_type, []).append(line)
        self._s["bytes"] += len(line)
        return len(line)

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


class InMemorySpool:
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


class PassthroughFormatter:
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


class MemorySink:
    accepts = frozenset({"collection", "batch", "document", "metadata"})

    def __init__(self, fail_on: str | None = None):
        self.delivered: dict[JobId, list[Unit]] = {}
        self.aborted: list[tuple[JobId, str]] = []
        self.fail_on = fail_on  # unit name that raises, to test retries

    def begin(self, job: Job):
        sink = self

        class Session:
            def __init__(self):
                self.units: list[Unit] = []

            def write(self, unit: Unit, spool) -> UnitOutcome:
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
