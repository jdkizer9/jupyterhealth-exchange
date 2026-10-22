"""CompositionBundleFormatter: one FHIR `document` Bundle per pull, plus a metadata companion (design §4.1).

Every resource gets a `urn:uuid` fullUrl (deterministic per type/id, so a re-run produces the same
bundle), internal references are rewritten to those urns, and a Composition lists the content by
section. References to things the pull did not include (Practitioner, Organization, Location...)
are left as the EHR wrote them, made absolute against the iss so they stay meaningful.

Documents (M5) travel as sidecar blobs: the attachment's `url` points at `docs/{id}` relative to
the bundle, the original EHR URL is kept in an extension, inline `data` is moved out.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from patient_access.core.interfaces import SpoolHandle
from patient_access.core.models import BlobRef, Job, Unit

NS = uuid.UUID("6f1c7d0e-3b0a-4c5e-9a57-1d2c0b7e9a10")
ORIGINAL_URL_EXT = "https://jupyterhealth.org/fhir/StructureDefinition/original-attachment-url"
LOINC_SUMMARY = {"system": "http://loinc.org", "code": "60591-5", "display": "Patient summary Document"}

SECTION_TITLES = {
    "AllergyIntolerance": "Allergies and intolerances",
    "Condition": "Problems",
    "MedicationRequest": "Medications",
    "MedicationDispense": "Medication dispenses",
    "Immunization": "Immunizations",
    "Procedure": "Procedures",
    "Observation": "Results and vital signs",
    "DiagnosticReport": "Diagnostic reports",
    "DocumentReference": "Clinical notes",
    "Encounter": "Encounters",
    "CarePlan": "Care plans",
    "CareTeam": "Care teams",
    "Goal": "Goals",
    "ServiceRequest": "Service requests",
    "Device": "Devices",
    "QuestionnaireResponse": "Questionnaire responses",
}


def urn(resource_type: str, rid: str) -> str:
    return f"urn:uuid:{uuid.uuid5(NS, f'{resource_type}/{rid}')}"


def _rewrite_refs(node: Any, index: dict[str, str], iss: str) -> Any:
    """Rewrite every `reference` that names a resource in the bundle; absolutise the rest."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "reference" and isinstance(v, str) and not v.startswith(("#", "urn:")):
                rel = v[len(iss) + 1 :] if v.startswith(iss + "/") else v
                parts = rel.split("/")
                key = "/".join(parts[-2:]) if len(parts) >= 2 else rel
                out[k] = index.get(key) or (v if "://" in v else f"{iss}/{rel}")
            else:
                out[k] = _rewrite_refs(v, index, iss)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(v, index, iss) for v in node]
    return node


class CompositionBundleFormatter:
    produces = frozenset({"document", "metadata"})

    def __init__(self, author_name: str = "Patient Access Module"):
        self.author_name = author_name

    def format(self, spool: SpoolHandle, job: Job) -> Iterator[Unit]:
        iss = job.source.iss.rstrip("/")
        index_info = spool.index()
        types = [t for t in index_info.types if index_info.types[t]]
        # Pass 1: ids only, to know what references can be resolved inside the bundle.
        index: dict[str, str] = {}
        for t in types:
            for r in spool.iter_type(t):
                if r.get("id"):
                    index[f"{t}/{r['id']}"] = urn(t, r["id"])
        now = datetime.now(UTC).isoformat(timespec="seconds")
        device_urn = f"urn:uuid:{uuid.uuid5(NS, 'author/' + self.author_name)}"
        patient_key = f"Patient/{job.source.ehr_patient_id}"

        blobs: list[BlobRef] = []
        blob_meta = index_info.blobs
        entries: list[dict] = []
        sections: dict[str, list[dict]] = {}
        count = 0
        for t in types:
            for r in spool.iter_type(t):
                r = _rewrite_refs(r, index, iss)
                if t == "DocumentReference":
                    r = self._point_at_sidecars(r, "content", blob_meta, blobs)
                elif t == "DiagnosticReport":
                    r = self._point_at_sidecars(r, "presentedForm", blob_meta, blobs)
                full = index.get(f"{t}/{r.get('id')}") or f"urn:uuid:{uuid.uuid4()}"
                entries.append({"fullUrl": full, "resource": r})
                if t != "Patient":
                    sections.setdefault(t, []).append({"reference": full})
                count += 1

        composition = {
            "resourceType": "Composition",
            "id": str(uuid.uuid5(NS, f"composition/{job.id}")),
            "status": "final",
            "type": {"coding": [LOINC_SUMMARY]},
            "subject": {"reference": index.get(patient_key, f"{iss}/{patient_key}")},
            "date": now,
            "author": [{"reference": device_urn}],
            "title": f"Records shared from {job.source.brand_name}",
            "section": [
                {"title": SECTION_TITLES.get(t, t), "code": {"text": t}, "entry": refs} for t, refs in sections.items()
            ],
        }
        device = {
            "resourceType": "Device",
            "id": str(uuid.uuid5(NS, "author/" + self.author_name)),
            "deviceName": [{"name": self.author_name, "type": "user-friendly-name"}],
        }
        bundle = {
            "resourceType": "Bundle",
            "type": "document",
            "identifier": {"system": "urn:ietf:rfc:3986", "value": f"urn:uuid:{uuid.uuid5(NS, 'bundle/' + job.id)}"},
            "timestamp": now,
            "entry": [
                {"fullUrl": f"urn:uuid:{composition['id']}", "resource": composition},
                {"fullUrl": device_urn, "resource": device},
                *entries,
            ],
        }
        yield Unit(name="bundle", kind="document", body=bundle, blobs=blobs, resources=count)
        yield Unit(name="metadata", kind="metadata", body=self.metadata(job, count, blobs, now))

    @staticmethod
    def _point_at_sidecars(r: dict, field: str, blob_meta: dict, blobs: list[BlobRef]) -> dict:
        items = r.get(field) or []
        for i, item in enumerate(items):
            att = item.get("attachment", item) if field == "content" else item
            blob_id = f"{r['resourceType']}-{r.get('id')}-{i}"
            meta = blob_meta.get(blob_id)
            if meta is None:
                continue
            key = f"docs/{blob_id}"
            if att.get("url"):
                att.setdefault("extension", []).append({"url": ORIGINAL_URL_EXT, "valueUrl": att["url"]})
            att.pop("data", None)
            att["url"] = key
            att["size"] = meta.size
            blobs.append(BlobRef(blob_id=blob_id, key=key, meta=meta))
        return r

    @staticmethod
    def metadata(job: Job, count: int, blobs: list[BlobRef], now: str) -> dict:
        return {
            "job": job.id,
            "participant": {
                "ref": job.participant.ref,
                "provider": job.participant.provider,
                "attrs": job.participant.attrs,
                "link_id": job.participant.handle or None,
            },
            "source": {
                "brand": job.source.brand_name,
                "location": job.source.location_name,
                "iss": job.source.iss,
                "vendor": job.source.vendor,
                "ehr_patient_id": job.source.ehr_patient_id,
                "scopes_granted": job.source.scopes_granted,
            },
            "generated_at": now,
            "resources": count,
            "outcomes": [
                {
                    "label": o.label,
                    "type": o.resource_type,
                    "status": o.status,
                    "count": o.count,
                    "error": o.error,
                    "capped": o.capped,
                }
                for o in job.outcomes
            ],
            "documents": {
                "status": job.documents.status,
                "fetched": job.documents.fetched,
                "skipped": job.documents.skipped,
                "items": [
                    {
                        "key": b.key,
                        "content_type": b.meta.content_type,
                        "bytes": b.meta.size,
                        "sha256": b.meta.sha256,
                        "referenced_by": b.meta.referenced_by,
                    }
                    for b in blobs
                ],
            },
        }
