"""LocalDiskSpool: one directory per job on worker-local disk, every byte encrypted with the job's key.

Layout under {root}/{job_id}/:
    {Type}.ndjson     one Fernet token per line, each the JSON of one resource
    blobs/{blob_id}   one Fernet token, the blob's bytes
    blobs.json        Fernet token of {blob_id: BlobMeta}
Without the job's data key (held, encrypted, on the job row and cleared at a terminal state) a
leftover directory is unreadable: crypto-shred (design §8). delete() and sweep() remove the bytes too.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from cryptography.fernet import Fernet

from patient_access.core.interfaces import Spool, SpoolHandle
from patient_access.core.models import BlobMeta, Job, SpoolIndex

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(name: str) -> str:
    return _SAFE.sub("_", name)[:200]


class LocalDiskSpoolHandle(SpoolHandle):
    def __init__(self, ref: str, path: Path, fernet: Fernet):
        self.ref = ref
        self.path = path
        self._f = fernet
        (path / "blobs").mkdir(parents=True, exist_ok=True)
        self._files: dict[str, BinaryIO] = {}
        self._blob_meta: dict[str, BlobMeta] = self._load_blob_meta()

    # --- resources

    def _type_path(self, resource_type: str) -> Path:
        return self.path / f"{_safe(resource_type)}.ndjson"

    def append(self, resource_type: str, resource: dict) -> int:
        f = self._files.get(resource_type)
        if f is None:
            f = self._files[resource_type] = open(self._type_path(resource_type), "ab")  # noqa: SIM115
        line = self._f.encrypt(json.dumps(resource, separators=(",", ":")).encode()) + b"\n"
        f.write(line)
        return len(line)

    def _flush(self) -> None:
        for f in self._files.values():
            f.flush()

    def flush(self, resource_type: str) -> None:
        f = self._files.get(resource_type)
        if f is not None:
            f.flush()
            os.fsync(f.fileno())

    def has_resource(self, resource_type: str, resource_id: str) -> bool:
        return any(r.get("id") == resource_id for r in self.iter_type(resource_type))

    def reset_type(self, resource_type: str) -> None:
        f = self._files.pop(resource_type, None)
        if f:
            f.close()
        self._type_path(resource_type).unlink(missing_ok=True)

    def iter_type(self, resource_type: str) -> Iterator[dict]:
        self._flush()
        p = self._type_path(resource_type)
        if not p.exists():
            return
        with open(p, "rb") as f:
            for line in f:
                if line.strip():
                    yield json.loads(self._f.decrypt(line.strip()))

    # --- blobs

    def _load_blob_meta(self) -> dict[str, BlobMeta]:
        p = self.path / "blobs.json"
        if not p.exists():
            return {}
        return {k: BlobMeta(**v) for k, v in json.loads(self._f.decrypt(p.read_bytes())).items()}

    def put_blob(self, blob_id: str, data: bytes, meta: BlobMeta) -> None:
        (self.path / "blobs" / _safe(blob_id)).write_bytes(self._f.encrypt(data))
        self._blob_meta[blob_id] = meta
        payload = json.dumps({k: asdict(v) for k, v in self._blob_meta.items()}).encode()
        (self.path / "blobs.json").write_bytes(self._f.encrypt(payload))

    def get_blob(self, blob_id: str) -> bytes:
        return self._f.decrypt((self.path / "blobs" / _safe(blob_id)).read_bytes())

    # --- index

    def index(self) -> SpoolIndex:
        self._flush()
        types = {}
        for p in sorted(self.path.glob("*.ndjson")):
            with open(p, "rb") as f:
                types[p.stem] = sum(1 for line in f if line.strip())
        total = sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())
        return SpoolIndex(types=types, blobs=dict(self._blob_meta), bytes=total)

    def close(self) -> None:
        for f in self._files.values():
            f.close()
        self._files.clear()


class LocalDiskSpool(Spool):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def open(self, job: Job, data_key: bytes) -> LocalDiskSpoolHandle:
        ref = job.spool_ref or _safe(job.id)
        return LocalDiskSpoolHandle(ref, self.root / ref, Fernet(base64.urlsafe_b64encode(data_key)))

    def delete(self, spool_ref: str) -> None:
        shutil.rmtree(self.root / _safe(spool_ref), ignore_errors=True)

    def sweep(self, keep: set[str], older_than: timedelta) -> list[str]:
        cutoff = time.time() - older_than.total_seconds()
        gone = []
        for p in self.root.iterdir():
            if p.is_dir() and p.name not in keep and os.stat(p).st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                gone.append(p.name)
        return gone
