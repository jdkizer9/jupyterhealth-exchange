"""Phase 4, second pass: document content referenced by the fetched resources (design §6).

Runs after every resource type, under the same token, so caps can be applied knowing the totals
and a timeout here never costs the resource bundle. Sources:
    DocumentReference.content[].attachment   clinical notes
    DiagnosticReport.presentedForm[]         report renderings
Each attachment is either inline (`data`, base64) or a URL, usually `Binary/{id}`. One content
item per DocumentReference is kept: the first in the vendor's preference order. Blob ids are
`{Type}-{id}-{index}`, which is what the formatter looks for.

C-CDA via `$docref` is out of scope for the sketch (no sandbox offers it to a patient app).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import requests

from .interfaces import SpoolHandle
from .models import BlobMeta, Job
from .vendors import DocumentPolicy, VendorConfig

if TYPE_CHECKING:
    from .puller import FhirClient

log = logging.getLogger(__name__)


def _choose(items: list[dict], prefer: tuple[str, ...]) -> list[tuple[int, dict]]:
    """For DocumentReference: one item, by content-type preference. Unknown types rank last."""
    if not items:
        return []

    def rank(pair: tuple[int, dict]) -> tuple[int, int]:
        ctype = (pair[1].get("contentType") or "").split(";")[0].strip().lower()
        return (prefer.index(ctype) if ctype in prefer else len(prefer), pair[0])

    return [min(enumerate(items), key=rank)]


def _candidates(spool: SpoolHandle, policy: DocumentPolicy):
    if "DocumentReference" in policy.sources:
        for r in spool.iter_type("DocumentReference"):
            atts = [c.get("attachment") or {} for c in r.get("content") or []]
            for i, att in _choose(atts, policy.prefer):
                yield r, i, att
    if "DiagnosticReport" in policy.sources:
        for r in spool.iter_type("DiagnosticReport"):
            for i, att in enumerate(r.get("presentedForm") or []):
                yield r, i, att


def _download(client: FhirClient, url: str, ctype: str, max_bytes: int) -> tuple[bytes, str] | str:
    """Bytes and content type, or a skip reason."""
    if "://" not in url:
        url = f"{client.base}/{url.lstrip('/')}"
    elif not url.startswith(client.base):
        # Never send the EHR token to another host.
        return "url outside the FHIR server"
    client.ensure_valid()
    r = client.http.get(url, headers={"Accept": ctype or "*/*"}, timeout=client.vendor.timeout_seconds, stream=True)
    client.requests += 1
    try:
        if r.status_code != 200:
            return f"http {r.status_code}"
        declared = int(r.headers.get("Content-Length") or 0)
        if declared > max_bytes:
            return "over per-document size cap"
        chunks, size = [], 0
        for chunk in r.iter_content(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                return "over per-document size cap"
            chunks.append(chunk)
        data = b"".join(chunks)
        got = (r.headers.get("Content-Type") or ctype or "application/octet-stream").split(";")[0].strip()
    finally:
        r.close()
    if got in ("application/fhir+json", "application/json"):
        # The server ignored Accept and returned a FHIR Binary resource.
        try:
            body = json.loads(data)
        except ValueError:
            return "unreadable Binary"
        if body.get("resourceType") == "Binary":
            try:
                return base64.b64decode(body.get("data") or ""), body.get("contentType") or ctype
            except binascii.Error:
                return "unreadable Binary"
    return data, got


def fetch_documents(
    job: Job, spool: SpoolHandle, vendor: VendorConfig, client: FhirClient, save: Callable[[], None]
) -> None:
    policy, out = vendor.documents, job.documents
    if not policy.enabled:
        out.status = "skipped"
        save()
        return
    out.status, out.candidates, out.fetched, out.inline, out.bytes, out.skipped, out.error = (
        "pending",
        0,
        0,
        0,
        0,
        {},
        None,
    )

    def skip(reason: str) -> None:
        out.skipped[reason] = out.skipped.get(reason, 0) + 1

    from .puller import TokenExpired  # local import: puller imports this module

    for resource, i, att in _candidates(spool, policy):
        out.candidates += 1
        blob_id = f"{resource['resourceType']}-{resource.get('id')}-{i}"
        if out.fetched >= policy.max_count:
            skip("over document count cap")
            continue
        ctype = att.get("contentType") or ""
        if att.get("data"):
            try:
                data = base64.b64decode(att["data"])
            except binascii.Error:
                skip("bad inline data")
                continue
            source_url, inline = None, True
        elif att.get("url"):
            try:
                result = _download(client, att["url"], ctype, policy.max_bytes_each)
            except TokenExpired:
                out.error = "authorization expired during document download"
                break
            except requests.RequestException as e:
                skip(f"network: {type(e).__name__}")
                continue
            if isinstance(result, str):
                skip(result)
                continue
            data, ctype = result
            source_url, inline = att["url"], False
        else:
            skip("no data or url")
            continue
        if len(data) > policy.max_bytes_each:
            skip("over per-document size cap")
            continue
        if out.bytes + len(data) > policy.max_total_bytes:
            skip("over total size cap")
            continue
        spool.put_blob(
            blob_id,
            data,
            BlobMeta(
                content_type=ctype or "application/octet-stream",
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                source_url=source_url,
                referenced_by=f"{resource['resourceType']}/{resource.get('id')}",
            ),
        )
        out.fetched += 1
        out.inline += inline
        out.bytes += len(data)
        if out.fetched % 10 == 0:
            save()
    out.status = "error" if out.error else "ok"
    save()
