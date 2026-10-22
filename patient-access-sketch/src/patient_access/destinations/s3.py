"""S3Sink: pulls/{participant ref}/{job}/bundle.json, metadata.json, docs/{id} (design §4.1).

Works against AWS or any S3-compatible store (Silo in the sketch). Server-side encryption is
requested when `sse` is set ("aws:kms" with a key id in production; Silo dev uses none). abort()
deletes what this session wrote, so a consumer listing the prefix never sees a partial pull; the
metadata object is written last and is the "complete" marker.
"""

from __future__ import annotations

import json

import boto3
from botocore.config import Config

from patient_access.core.models import Job, Receipt, Unit, UnitOutcome

from .local_dir import safe


class S3Sink:
    accepts = frozenset({"document", "batch", "collection", "metadata"})

    def __init__(
        self,
        bucket: str,
        prefix: str = "pulls",
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        sse: str | None = None,
        kms_key_id: str | None = None,
        create_bucket: bool = False,
    ):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.sse = sse
        self.kms_key_id = kms_key_id
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"}),
        )
        self.create_bucket = create_bucket
        self._ready = False

    def _ensure_bucket(self) -> None:
        if self._ready or not self.create_bucket:
            return
        existing = {b["Name"] for b in self.client.list_buckets().get("Buckets", [])}
        if self.bucket not in existing:
            self.client.create_bucket(Bucket=self.bucket)
        self._ready = True

    def begin(self, job: Job) -> S3Session:
        self._ensure_bucket()
        return S3Session(self, f"{self.prefix}/{safe(job.participant.ref)}/{safe(job.id)}")


class S3Session:
    def __init__(self, sink: S3Sink, prefix: str):
        self.sink = sink
        self.prefix = prefix
        self.keys: list[str] = []
        self.units = self.resources = self.blobs = self.bytes = 0

    def _put(self, rel: str, data: bytes, content_type: str) -> None:
        extra = {}
        if self.sink.sse:
            extra["ServerSideEncryption"] = self.sink.sse
            if self.sink.kms_key_id:
                extra["SSEKMSKeyId"] = self.sink.kms_key_id
        key = f"{self.prefix}/{rel}"
        self.sink.client.put_object(Bucket=self.sink.bucket, Key=key, Body=data, ContentType=content_type, **extra)
        self.keys.append(key)
        self.bytes += len(data)

    def write(self, unit: Unit, spool) -> UnitOutcome:
        for blob in unit.blobs:
            self._put(blob.key, spool.get_blob(blob.blob_id), blob.meta.content_type)
            self.blobs += 1
        data = json.dumps(unit.body, separators=(",", ":")).encode()
        ctype = "application/json" if unit.kind == "metadata" else "application/fhir+json"
        self._put(f"{safe(unit.name)}.json", data, ctype)
        self.units += 1
        self.resources += unit.resources
        return UnitOutcome(ok=unit.resources, bytes=len(data))

    def finish(self) -> Receipt:
        return Receipt(
            destination="s3",
            location=f"s3://{self.sink.bucket}/{self.prefix}/",
            units=self.units,
            resources=self.resources,
            blobs=self.blobs,
            bytes=self.bytes,
            details={"keys": len(self.keys)},
        )

    def abort(self, reason: str) -> None:
        for i in range(0, len(self.keys), 1000):
            chunk = self.keys[i : i + 1000]
            self.sink.client.delete_objects(
                Bucket=self.sink.bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
            )
