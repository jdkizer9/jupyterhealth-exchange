"""Brands Bundle importer. Copied from JHE core/management/commands/import_ehr_brands.py (2026-10-22)
with one change: the vendor is a parameter (JHE guessed Epic Sandbox vs Epic Production from the
brand name), since the directory must work for any vendor (report §4 #2)."""

from django.db import transaction

from patient_access.core.models import ImportCounts

from .models import EhrBrand, EhrBrandLocation, EhrVendor


def _first_endpoint_address(org, endpoints):
    for ref in org.get("endpoint", []) or []:
        reference = ref.get("reference", "")
        endpoint = endpoints.get(reference) or endpoints.get(reference.split("/")[-1])
        if endpoint and endpoint.get("address"):
            return endpoint["address"]
    return None


def _local_ref_id(reference):
    if reference.startswith("urn:uuid:"):
        return reference[len("urn:uuid:") :]
    return reference.split("/")[-1]


def _npi_type_2(org):
    for ident in org.get("identifier", []) or []:
        if "us-npi" in (ident.get("system") or ""):
            return ident.get("value")
    return None


def _address_parts(org):
    addresses = org.get("address", []) or []
    if not addresses:
        return {"address_text": "", "city": None, "state": None, "postal_code": None, "country": None}
    a = addresses[0]
    text = a.get("text")
    if not text:
        text = ", ".join(p for p in [*(a.get("line") or []), a.get("city"), a.get("state"), a.get("postalCode")] if p)
    return {
        "address_text": text or "",
        "city": a.get("city"),
        "state": a.get("state"),
        "postal_code": a.get("postalCode"),
        "country": a.get("country"),
    }


@transaction.atomic
def import_brands_bundle(bundle: dict, vendor: EhrVendor) -> ImportCounts:
    resources = [e.get("resource", {}) for e in bundle.get("entry", []) or []]
    endpoints = {}
    for entry in bundle.get("entry", []) or []:
        res = entry.get("resource", {})
        if res.get("resourceType") == "Endpoint":
            endpoints[f"Endpoint/{res.get('id')}"] = res
            endpoints[res.get("id")] = res
            if entry.get("fullUrl"):
                endpoints[entry["fullUrl"]] = res

    orgs = [r for r in resources if r.get("resourceType") == "Organization"]
    brand_orgs = [o for o in orgs if o.get("endpoint")]
    facility_orgs = [o for o in orgs if o.get("partOf") and not o.get("endpoint")]

    brand_by_org_id = {}
    brand_count = 0
    for org in brand_orgs:
        base_url = _first_endpoint_address(org, endpoints)
        if not base_url:
            continue
        brand, _ = EhrBrand.objects.update_or_create(
            fhir_base_url=base_url,
            defaults={"name": org.get("name") or base_url, "npi_type_2": _npi_type_2(org), "vendor": vendor},
        )
        brand_by_org_id[org.get("id")] = (brand, org)
        brand_count += 1

    location_count = 0
    brands_with_locations = set()
    for org in facility_orgs:
        brand_entry = brand_by_org_id.get(_local_ref_id((org.get("partOf") or {}).get("reference", "")))
        if not brand_entry:
            continue
        brand, _ = brand_entry
        parts = _address_parts(org)
        _, created = EhrBrandLocation.objects.get_or_create(
            brand=brand, name=org.get("name") or brand.name, address_text=parts["address_text"], defaults=parts
        )
        brands_with_locations.add(brand.id)
        location_count += created

    for brand, org in brand_by_org_id.values():
        if brand.id in brands_with_locations:
            continue
        parts = _address_parts(org)
        _, created = EhrBrandLocation.objects.get_or_create(
            brand=brand, name=brand.name, address_text=parts["address_text"], defaults=parts
        )
        location_count += created

    return ImportCounts(brands=brand_count, locations=location_count)


def brands_bundle(brands: list[dict]) -> dict:
    """A minimal user-access Brands Bundle from [{id, name, iss, city, state, postal, line}]."""
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
        entries.append(
            {"resource": {"resourceType": "Endpoint", "id": f"{b['id']}-ep", "address": b["iss"], "status": "active"}}
        )
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}
