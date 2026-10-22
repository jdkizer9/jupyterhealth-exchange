"""Phase 4: walk the vendor's query table, page every search, append to the spool (design §3.3, §5).

Per-type isolation: one type's failure is recorded on its outcome and the walk continues. A 401
means the token is gone and nothing else will work, so the walk stops. Resume is per resource
type: a type is re-fetched from scratch unless every query table row for it finished.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import requests

from .documents import fetch_documents
from .http import session as http_session
from .interfaces import SpoolHandle
from .models import Job, TypeOutcome
from .pipeline import EHR_TOKEN, FetchAborted
from .vendors import QuerySpec, VendorConfig

log = logging.getLogger(__name__)

EXPIRY_MARGIN = timedelta(seconds=30)
SAVE_EVERY_PAGES = 5


class TokenExpired(Exception):
    pass


class FhirClient:
    """Just enough FHIR REST for a patient pull: GET with bearer auth, follow `next` links."""

    def __init__(
        self,
        base: str,
        token: str,
        vendor: VendorConfig,
        expires_at: datetime | None,
        http: requests.Session | None = None,
    ):
        self.base = base.rstrip("/")
        self.vendor = vendor
        self.expires_at = expires_at
        self.http = http or http_session(vendor.max_retries)
        self.http.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"})
        self.requests = 0

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        if self.expires_at and datetime.now(UTC) >= self.expires_at - EXPIRY_MARGIN:
            raise TokenExpired()
        self.requests += 1
        r = self.http.get(url, params=params, timeout=self.vendor.timeout_seconds)
        if r.status_code == 401:
            raise FetchAborted("The health system stopped accepting our access (401). Please connect again.")
        return r


def _search_url(base: str, spec: QuerySpec, patient: str, page_size: int | None) -> tuple[str, dict]:
    path, _, query = spec.query.partition("?")
    if spec.single:
        return f"{base}/{path}/{patient}", {}
    params = dict(parse_qsl(query, keep_blank_values=True))
    params["patient"] = patient  # every search is patient-scoped here, so the JS explicitPatient quirk is moot
    if page_size:
        params["_count"] = str(page_size)
    return f"{base}/{path}", params


def _error_text(r: requests.Response) -> str:
    try:
        body = r.json()
        if body.get("resourceType") == "OperationOutcome":
            issue = (body.get("issue") or [{}])[0]
            text = issue.get("diagnostics") or issue.get("details", {}).get("text") or issue.get("code")
            return f"{r.status_code} {text}"
    except ValueError:
        pass
    return f"{r.status_code} {r.reason}"


def _pull(
    client: FhirClient,
    spec: QuerySpec,
    patient: str,
    spool: SpoolHandle,
    outcome: TypeOutcome,
    seen: set[str],
    save: Callable[[], None],
) -> None:
    url, params = _search_url(client.base, spec, patient, client.vendor.page_size)
    started = time.monotonic()
    while url:
        if outcome.pages >= client.vendor.page_cap:
            outcome.capped = True
            break
        r = client.get(url, params)
        params = None  # `next` links carry their own query
        if r.status_code != 200:
            outcome.status = "error"
            outcome.error = _error_text(r)
            return
        body = r.json()
        outcome.pages += 1
        entries = [body] if spec.single else [e.get("resource") or {} for e in body.get("entry") or []]
        for resource in entries:
            if resource.get("resourceType") != spec.resource_type:
                continue  # OperationOutcome warnings, _include'd resources
            rid = resource.get("id")
            if rid and rid in seen:
                continue  # e.g. an Observation in both Labs and Vital Signs
            if rid:
                seen.add(rid)
            outcome.bytes += spool.append(spec.resource_type, resource)
            outcome.count += 1
        url = (
            None
            if spec.single
            else next((link.get("url") for link in body.get("link") or [] if link.get("relation") == "next"), None)
        )
        if outcome.pages % SAVE_EVERY_PAGES == 0:
            save()
    outcome.status = "ok"
    outcome.seconds = round(time.monotonic() - started, 2)


def fetch(
    job: Job, spool: SpoolHandle, vendor: VendorConfig, save: Callable[[], None], http: requests.Session | None = None
) -> None:
    client = FhirClient(job.source.iss, job.credentials[EHR_TOKEN], vendor, job.token_expires_at, http)
    patient = job.source.ehr_patient_id
    specs = {q.label: q for q in vendor.queries_for(job.source.scopes_granted)}
    if not job.outcomes:
        job.outcomes = [TypeOutcome(q.label, q.resource_type, q.query) for q in specs.values()]

    # Group the table by resource type: the spool is per type, so resume is too.
    by_type: OrderedDict[str, list[TypeOutcome]] = OrderedDict()
    for o in job.outcomes:
        by_type.setdefault(o.resource_type, []).append(o)

    started = time.monotonic()
    expired = False
    for resource_type, outcomes in by_type.items():
        if all(o.status in ("ok", "skipped") for o in outcomes):
            continue
        if expired:
            for o in outcomes:
                o.status, o.error = "error", "authorization expired before this was downloaded"
            continue
        spool.reset_type(resource_type)
        seen: set[str] = set()
        for o in outcomes:
            o.status, o.count, o.pages, o.bytes, o.error, o.capped = "pending", 0, 0, 0, None, False
            spec = specs.get(o.label)
            if spec is None:
                o.status, o.error = "skipped", "scope not granted"
                continue
            try:
                _pull(client, spec, patient, spool, o, seen, save)
            except TokenExpired:
                o.status, o.error = "error", "authorization expired during download"
                expired = True
                break
            except requests.RequestException as e:
                o.status, o.error = "error", f"network: {type(e).__name__}"
            except ValueError as e:  # bad JSON
                o.status, o.error = "error", f"unreadable response: {e}"[:200]
        save()

    if not expired:
        fetch_documents(job, spool, vendor, client, save)
    log.info(
        "job %s fetched %d resources in %d requests, %.1fs",
        job.id,
        sum(o.count for o in job.outcomes),
        client.requests,
        time.monotonic() - started,
    )
