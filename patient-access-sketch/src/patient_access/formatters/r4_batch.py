"""R4BatchFormatter: per resource type, `batch` Bundles of N entries, resources as fetched (design §4.2).

What JHE's `/fhir-import/R4/` takes today; documents are omitted (JHE has no Binary type, §6).
"""

from __future__ import annotations

from collections.abc import Iterator

from patient_access.core.interfaces import SpoolHandle
from patient_access.core.models import Job, Unit


class R4BatchFormatter:
    produces = frozenset({"batch"})

    def __init__(self, chunk: int = 100, skip_types: list[str] | None = None):
        self.chunk = chunk
        self.skip_types = set(skip_types or [])

    def format(self, spool: SpoolHandle, job: Job) -> Iterator[Unit]:
        for resource_type, count in spool.index().types.items():
            if not count or resource_type in self.skip_types:
                continue
            batch: list[dict] = []
            n = 0
            for resource in spool.iter_type(resource_type):
                batch.append({"resource": resource})
                if len(batch) == self.chunk:
                    yield self._unit(resource_type, n, batch)
                    batch, n = [], n + 1
            if batch:
                yield self._unit(resource_type, n, batch)

    @staticmethod
    def _unit(resource_type: str, n: int, entries: list[dict]) -> Unit:
        return Unit(
            name=f"{resource_type}-{n:04d}",
            kind="batch",
            body={"resourceType": "Bundle", "type": "batch", "entry": entries},
            resources=len(entries),
        )
