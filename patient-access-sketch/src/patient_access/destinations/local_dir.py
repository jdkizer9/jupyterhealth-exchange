"""LocalDirectorySink: the S3 layout on local disk, for development and inspection.

{root}/pulls/{participant ref}/{job id}/{unit}.json and docs/{blob}. Each file is written to a temp
name and renamed, and abort removes the job's directory, so a reader never sees half a pull.
Not encrypted: it stands in for a destination, and destinations own their at-rest policy.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from patient_access.core.interfaces import Sink, SinkSession, SpoolHandle
from patient_access.core.models import Job, Receipt, Unit, UnitOutcome

from .layout import KeyLayout, safe


class LocalDirectorySink(Sink):
    accepts = frozenset({"document", "batch", "collection", "metadata"})

    def __init__(self, root: str, layout: str = KeyLayout.DEFAULT):
        self.root = Path(root)
        self.layout = KeyLayout(layout)

    def begin(self, job: Job) -> LocalDirectorySession:
        return LocalDirectorySession(self.root / self.layout.render(job), job)


class LocalDirectorySession(SinkSession):
    def __init__(self, path: Path, job: Job):
        self.path = path
        self.job = job
        if path.exists():
            shutil.rmtree(path)  # a retry starts clean
        path.mkdir(parents=True)
        self.units = self.resources = self.blobs = self.bytes = 0

    def _put(self, rel: str, data: bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.rename(target)
        self.bytes += len(data)

    def write(self, unit: Unit, spool: SpoolHandle) -> UnitOutcome:
        for blob in unit.blobs:
            self._put(blob.key, spool.get_blob(blob.blob_id))
            self.blobs += 1
        data = json.dumps(unit.body, separators=(",", ":")).encode()
        self._put(f"{safe(unit.name)}.json", data)
        self.units += 1
        self.resources += unit.resources
        return UnitOutcome(ok=unit.resources, bytes=len(data))

    def finish(self) -> Receipt:
        return Receipt(
            destination="local",
            location=str(self.path),
            units=self.units,
            resources=self.resources,
            blobs=self.blobs,
            bytes=self.bytes,
        )

    def abort(self, reason: str) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
