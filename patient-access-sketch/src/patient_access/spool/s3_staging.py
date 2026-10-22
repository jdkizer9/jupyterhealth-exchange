"""S3StagingSpool: the spool on an S3 prefix, so the worker is stateless (design §7.2, §8).

    {prefix}/{job}/types/{Type}/{n:06d}   Fernet token of an NDJSON chunk (up to `chunk` resources)
    {prefix}/{job}/blobs/{blob_id}        Fernet token of the blob
    {prefix}/{job}/blobs.json             Fernet token of {blob_id: BlobMeta}

S3 cannot append, so resources are buffered per type and flushed as numbered chunk objects; a
crash loses at most the unflushed buffer, and resume redoes the whole type anyway. Every object is
encrypted with the job's data key (crypto-shred), and the bucket carries a lifecycle rule that
expires the staging prefix, so even a spool nobody sweeps disappears on its own.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet

from patient_access.core.models import BlobMeta, Job, SpoolIndex

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(name: str) -> str:
    return _SAFE.sub("_", name)[:200]


class S3StagingSpoolHandle:
    def __init__(self, spool: S3StagingSpool, ref: str, fernet: Fernet):
        self.spool, self.ref, self._f = spool, ref, fernet
        self.root = f"{spool.prefix}/{ref}"
        self._buf: dict[str, list[bytes]] = {}
        self._next: dict[str, int] = {}
        self._blob_meta = self._load_blob_meta()

    @property
    def _c(self):
        return self.spool.client

    def _keys(self, prefix: str) -> list[tuple[str, int]]:
        out = []
        for page in self._c.get_paginator("list_objects_v2").paginate(Bucket=self.spool.bucket, Prefix=prefix):
            out += [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
        return sorted(out)

    def _put(self, key: str, data: bytes) -> None:
        self._c.put_object(Bucket=self.spool.bucket, Key=key, Body=self._f.encrypt(data))

    def _get(self, key: str) -> bytes:
        return self._f.decrypt(self._c.get_object(Bucket=self.spool.bucket, Key=key)["Body"].read())

    # --- resources

    def append(self, resource_type: str, resource: dict) -> int:
        line = json.dumps(resource, separators=(",", ":")).encode()
        buf = self._buf.setdefault(resource_type, [])
        buf.append(line)
        if len(buf) >= self.spool.chunk:
            self._flush(resource_type)
        return len(line)

    def _flush(self, resource_type: str | None = None) -> None:
        for t in [resource_type] if resource_type else list(self._buf):
            lines = self._buf.get(t)
            if not lines:
                continue
            if t not in self._next:
                self._next[t] = len(self._keys(f"{self.root}/types/{_safe(t)}/"))
            self._put(f"{self.root}/types/{_safe(t)}/{self._next[t]:06d}", b"\n".join(lines))
            self._next[t] += 1
            self._buf[t] = []

    def flush(self, resource_type: str) -> None:
        self._flush(resource_type)

    def has_resource(self, resource_type: str, resource_id: str) -> bool:
        return any(r.get("id") == resource_id for r in self.iter_type(resource_type))

    def reset_type(self, resource_type: str) -> None:
        self._buf.pop(resource_type, None)
        self._next.pop(resource_type, None)
        self.spool._delete_keys([k for k, _ in self._keys(f"{self.root}/types/{_safe(resource_type)}/")])

    def iter_type(self, resource_type: str) -> Iterator[dict]:
        self._flush(resource_type)
        for key, _ in self._keys(f"{self.root}/types/{_safe(resource_type)}/"):
            for line in self._get(key).split(b"\n"):
                if line:
                    yield json.loads(line)

    # --- blobs

    def _load_blob_meta(self) -> dict[str, BlobMeta]:
        try:
            raw = self._get(f"{self.root}/blobs.json")
        except ClientError:
            return {}
        return {k: BlobMeta(**v) for k, v in json.loads(raw).items()}

    def put_blob(self, blob_id: str, data: bytes, meta: BlobMeta) -> None:
        self._put(f"{self.root}/blobs/{_safe(blob_id)}", data)
        self._blob_meta[blob_id] = meta
        self._put(f"{self.root}/blobs.json", json.dumps({k: asdict(v) for k, v in self._blob_meta.items()}).encode())

    def get_blob(self, blob_id: str) -> bytes:
        return self._get(f"{self.root}/blobs/{_safe(blob_id)}")

    # --- index

    def index(self) -> SpoolIndex:
        self._flush()
        types: dict[str, int] = {}
        total = 0
        for key, size in self._keys(f"{self.root}/types/"):
            t = key.split("/")[-2]
            types[t] = types.get(t, 0) + sum(1 for line in self._get(key).split(b"\n") if line)
            total += size
        return SpoolIndex(types=types, blobs=dict(self._blob_meta), bytes=total)

    def close(self) -> None:
        self._flush()


class S3StagingSpool:
    def __init__(
        self,
        bucket: str,
        prefix: str = "staging",
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        chunk: int = 500,
        expire_days: int | None = 1,
        create_bucket: bool = False,
    ):
        self.bucket, self.prefix, self.chunk = bucket, prefix.strip("/"), int(chunk)
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(s3={"addressing_style": "path"}),
        )
        if create_bucket:
            existing = {b["Name"] for b in self.client.list_buckets().get("Buckets", [])}
            if bucket not in existing:
                self.client.create_bucket(Bucket=bucket)
        if expire_days:
            self.ensure_lifecycle(expire_days)

    def ensure_lifecycle(self, days: int) -> None:
        """The backstop: the store itself expires anything left under the staging prefix."""
        rule = {
            "ID": "expire-staging",
            "Status": "Enabled",
            "Filter": {"Prefix": f"{self.prefix}/"},
            "Expiration": {"Days": int(days)},
        }
        try:
            rules = self.client.get_bucket_lifecycle_configuration(Bucket=self.bucket).get("Rules", [])
        except ClientError:
            rules = []
        rules = [r for r in rules if r.get("ID") != "expire-staging"] + [rule]
        self.client.put_bucket_lifecycle_configuration(Bucket=self.bucket, LifecycleConfiguration={"Rules": rules})

    def open(self, job: Job, data_key: bytes) -> S3StagingSpoolHandle:
        ref = job.spool_ref or _safe(job.id)
        return S3StagingSpoolHandle(self, ref, Fernet(base64.urlsafe_b64encode(data_key)))

    def _delete_keys(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]], "Quiet": True}
            )

    def _refs(self) -> dict[str, datetime]:
        """Spool ref -> newest object time under it."""
        out: dict[str, datetime] = {}
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/"):
            for o in page.get("Contents", []):
                ref = o["Key"][len(self.prefix) + 1 :].split("/", 1)[0]
                out[ref] = max(out.get(ref, o["LastModified"]), o["LastModified"])
        return out

    def delete(self, spool_ref: str) -> None:
        keys = []
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=f"{self.prefix}/{_safe(spool_ref)}/"
        ):
            keys += [o["Key"] for o in page.get("Contents", [])]
        self._delete_keys(keys)

    def sweep(self, keep: set[str], older_than: timedelta) -> list[str]:
        cutoff = datetime.now(UTC) - older_than
        gone = [ref for ref, newest in self._refs().items() if ref not in keep and newest < cutoff]
        for ref in gone:
            self.delete(ref)
        return gone
