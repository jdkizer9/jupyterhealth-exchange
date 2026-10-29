"""Import a SMART User-access Brands Bundle (design §10.8): Endpoints (with their managing
organisation as the customer), brand Organizations (those with `endpoint`), facility
Organizations (those with `partOf`). Idempotent: endpoints keyed on address, brands on
(endpoint, source id), facilities on (brand, name, address).
"""

from __future__ import annotations

from django.db import transaction

from patient_access.core.models import ImportCounts

from .models import EhrBrand, EhrCustomer, EhrEndpoint, EhrFacility, EhrVendor


def _local_ref_id(reference: str) -> str:
    return reference[len("urn:uuid:") :] if reference.startswith("urn:uuid:") else reference.split("/")[-1]


def _npi_type_2(org: dict) -> str | None:
    for ident in org.get("identifier", []) or []:
        if "us-npi" in (ident.get("system") or ""):
            return ident.get("value")
    return None


def _address_parts(org: dict) -> dict:
    addresses = org.get("address", []) or []
    if not addresses:
        return {"address_text": "", "city": None, "state": None, "postal_code": None, "country": None}
    a = addresses[0]
    text = a.get("text") or ", ".join(
        p for p in [*(a.get("line") or []), a.get("city"), a.get("state"), a.get("postalCode")] if p
    )
    return {
        "address_text": text or "",
        "city": a.get("city"),
        "state": a.get("state"),
        "postal_code": a.get("postalCode"),
        "country": a.get("country"),
    }


def _fhir_version(ep: dict) -> str | None:
    for ext in ep.get("extension", []) or []:
        if (ext.get("url") or "").endswith("endpoint-fhir-version"):
            return ext.get("valueCode")
    return None


@transaction.atomic
def import_brands_bundle(bundle: dict, vendor: EhrVendor) -> ImportCounts:
    entries = bundle.get("entry", []) or []
    resources = [e.get("resource", {}) for e in entries]
    endpoints: dict[str, EhrEndpoint] = {}  # every way an Organization may reference the Endpoint
    for entry in entries:
        res = entry.get("resource", {})
        if res.get("resourceType") != "Endpoint" or not res.get("address"):
            continue
        customer = None
        mo = res.get("managingOrganization") or {}
        ext_id = (mo.get("identifier") or {}).get("value")
        if ext_id:
            customer, _ = EhrCustomer.objects.update_or_create(
                vendor=vendor, external_id=str(ext_id), defaults={"name": mo.get("display") or ""}
            )
        row, _ = EhrEndpoint.objects.update_or_create(
            address=res["address"],
            defaults={"vendor": vendor, "customer": customer, "fhir_version": _fhir_version(res)},
        )
        for key in (f"Endpoint/{res.get('id')}", res.get("id"), entry.get("fullUrl")):
            if key:
                endpoints[key] = row

    orgs = [r for r in resources if r.get("resourceType") == "Organization"]
    brand_by_org_id: dict[str, EhrBrand] = {}
    brands = 0
    for org in (o for o in orgs if o.get("endpoint")):
        ep = next(
            (endpoints.get(ref.get("reference", "")) for ref in org["endpoint"] if ref.get("reference") in endpoints),
            None,
        )
        if ep is None:
            continue
        brand, _ = EhrBrand.objects.update_or_create(
            endpoint=ep,
            source_id=org.get("id"),
            defaults={"name": org.get("name") or ep.address, "npi_type_2": _npi_type_2(org)},
        )
        brand_by_org_id[org.get("id")] = brand
        brands += 1

    facilities = 0
    for org in (o for o in orgs if o.get("partOf") and not o.get("endpoint")):
        parent = brand_by_org_id.get(_local_ref_id((org.get("partOf") or {}).get("reference", "")))
        if parent is None:
            continue
        parts = _address_parts(org)
        _, created = EhrFacility.objects.get_or_create(
            brand=parent, name=org.get("name") or parent.name, address_text=parts["address_text"], defaults=parts
        )
        facilities += created
    return ImportCounts(brands=brands, locations=facilities)


def brands_bundle(brands: list[dict]) -> dict:
    """A minimal User-access Brands Bundle from [{id, name, iss, city, state, postal, line, customer?}]."""
    entries = []
    for b in brands:
        entries.append(
            {
                "resource": {
                    "resourceType": "Organization",
                    "id": b["id"],
                    "name": b["name"],
                    "endpoint": [{"reference": f"Endpoint/{b['id']}-ep"}],
                    "address": [
                        {
                            "line": [b.get("line", "")],
                            "city": b.get("city"),
                            "state": b.get("state"),
                            "postalCode": b.get("postal"),
                        }
                    ],
                }
            }
        )
        ep = {"resourceType": "Endpoint", "id": f"{b['id']}-ep", "address": b["iss"], "status": "active"}
        if b.get("customer"):
            ep["managingOrganization"] = {"identifier": {"value": b["customer"]}, "display": b.get("customer_name", "")}
        entries.append({"resource": ep})
        if b.get("line") or b.get("city"):
            # A facility at the brand's own address, so the picker has a row with an address.
            entries.append(
                {
                    "resource": {
                        "resourceType": "Organization",
                        "id": f"{b['id']}-fac",
                        "name": b["name"],
                        "partOf": {"reference": f"Organization/{b['id']}"},
                        "address": entries[-2]["resource"]["address"],
                    }
                }
            )
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}
