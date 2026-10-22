"""JheHttpSink: deliver into a running JHE over its existing HTTP API, zero JHE changes (design §4.2).

begin:  POST /api/v1/ehr-patient-portal/identifier {system: iss, value: EHR patient id}
        POST /api/v1/fhir_sources {data_source, ehr_base_url}   (200 existing / 201 new)
write:  POST /fhir-import/R4/ (X-JHE-FHIR-Source-ID), tally per-entry outcomes from batch-response
finish: POST /o/revoke_token/ for the patient's JHE token; receipt with counts and reasons
Uses the participant's JHE token from `Participant.secrets` (the only component that reads it).
"""

from __future__ import annotations

import requests

from patient_access.core.http import session as http_session
from patient_access.core.models import Job, Receipt, Unit, UnitOutcome
from patient_access.identity.jhe_invitation import JHE_TOKEN


class JheError(Exception):
    pass


def _issues(entry: dict, severities: tuple[str, ...]) -> list[str]:
    issues = ((entry.get("response") or {}).get("outcome") or {}).get("issue") or []
    return [
        i.get("diagnostics") or (i.get("details") or {}).get("text") or i.get("code") or "unknown"
        for i in issues
        if i.get("severity") in severities
    ]


class JheHttpSink:
    accepts = frozenset({"batch"})

    def __init__(self, base_url: str, data_source_id: int | str, timeout: float = 120):
        self.base = base_url.rstrip("/")
        self.data_source_id = int(data_source_id)
        self.timeout = timeout

    def begin(self, job: Job) -> JheSession:
        token = job.participant.secrets.get(JHE_TOKEN)
        if not token:
            raise JheError("no JHE token for this participant (was it revoked by an earlier attempt?)")
        return JheSession(self, job, token)


class JheSession:
    def __init__(self, sink: JheHttpSink, job: Job, token: str):
        self.sink, self.job, self.token = sink, job, token
        self.http = http_session()
        self.http.headers.update({"Authorization": f"Bearer {token}", "Cache-Control": "no-cache"})
        self.ok = self.failed = self.units = self.bytes = 0
        self.reasons: dict[str, int] = {}
        self.warnings: dict[str, int] = {}
        r = self.http.post(
            f"{sink.base}/api/v1/ehr-patient-portal/identifier",
            json={"system": job.source.iss, "value": job.source.ehr_patient_id},
            timeout=sink.timeout,
        )
        if r.status_code not in (200, 201):
            raise JheError(f"identifier: HTTP {r.status_code} {r.text[:200]}")
        r = self.http.post(
            f"{sink.base}/api/v1/fhir_sources",
            json={"data_source": sink.data_source_id, "ehr_base_url": job.source.iss},
            timeout=sink.timeout,
        )
        if r.status_code not in (200, 201):
            raise JheError(f"fhir_sources: HTTP {r.status_code} {r.text[:200]}")
        self.source_id = r.json()["id"]
        self.source_created = r.status_code == 201

    def write(self, unit: Unit, spool) -> UnitOutcome:
        out = UnitOutcome()
        try:
            r = self.http.post(
                f"{self.sink.base}/fhir-import/R4/",
                json=unit.body,
                headers={"X-JHE-FHIR-Source-ID": str(self.source_id)},
                timeout=self.sink.timeout,
            )
        except requests.RequestException as e:
            raise JheError(f"import {unit.name}: {type(e).__name__}") from e
        if r.status_code >= 500 or r.status_code in (401, 429):
            raise JheError(f"import {unit.name}: HTTP {r.status_code}")  # retryable as a whole
        self.units += 1
        self.bytes += len(r.request.body or b"")
        if r.status_code != 200:
            reason = f"HTTP {r.status_code}: {r.text[:200]}"
            out.failed = unit.resources
            out.reasons[reason] = unit.resources
        else:
            for entry in r.json().get("entry") or []:
                status = str((entry.get("response") or {}).get("status") or "")
                if status.startswith("2"):
                    out.ok += 1
                    for w in _issues(entry, ("warning",)):
                        out.warnings[w] = out.warnings.get(w, 0) + 1
                else:
                    out.failed += 1
                    reason = next(iter(_issues(entry, ("error", "fatal"))), status or "unknown")
                    out.reasons[reason] = out.reasons.get(reason, 0) + 1
        self.ok += out.ok
        self.failed += out.failed
        for k, v in out.reasons.items():
            self.reasons[k] = self.reasons.get(k, 0) + v
        for k, v in out.warnings.items():
            self.warnings[k] = self.warnings.get(k, 0) + v
        return out

    def _revoke(self) -> bool:
        client_id = self.job.participant.attrs.get("jhe_client_id")
        r = self.http.post(
            f"{self.sink.base}/o/revoke_token/",
            data={"token": self.token, "token_type_hint": "access_token", "client_id": client_id},
            headers={"Authorization": ""},
            timeout=self.sink.timeout,
        )
        return r.status_code == 200

    def finish(self) -> Receipt:
        revoked = self._revoke()
        return Receipt(
            destination="jhe",
            location=f"FhirSource {self.source_id}",
            units=self.units,
            resources=self.ok,
            bytes=self.bytes,
            failures=dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])),
            warnings=dict(sorted(self.warnings.items(), key=lambda kv: -kv[1])),
            details={
                "fhir_source_id": self.source_id,
                "fhir_source_created": self.source_created,
                "rejected": self.failed,
                "jhe_token_revoked": revoked,
            },
        )

    def abort(self, reason: str) -> None:
        """Keep the token: a retry from the spool needs it. It expires on JHE's own schedule."""
