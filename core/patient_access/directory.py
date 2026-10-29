"""The module's Directory over JHE's EhrVendor / EhrBrand / EhrBrandLocation tables.

JHE's schema has three levels (vendor, brand-with-base-URL, location), so the module's endpoint
is the brand (one base URL each) and there is no customer; see Simon Q12 for the schema that
would carry both. Selection ids are JHE location pks, which is what the portal JS already sends.
Vendor settings come from the deployment config's `vendors` section, keyed by JHE's vendor names.
"""

from collections.abc import Mapping
from dataclasses import replace

from django.db.models import Q
from patient_access.core.interfaces import Directory
from patient_access.core.models import Brand, Endpoint, Facility, ImportCounts, Selection
from patient_access.core.source_settings import SourceSettings

from core.management.commands.import_ehr_brands import import_brands_bundle
from core.models import EhrBrandLocation, EhrVendor


def _selection(loc):
    brand = loc.brand
    endpoint = Endpoint(
        id=str(brand.id), iss=brand.fhir_base_url, vendor=brand.vendor.name, fhir_version=brand.fhir_version
    )
    return Selection(
        id=str(loc.id),
        brand=Brand(id=str(brand.id), name=brand.name, endpoint=endpoint, npi=brand.npi_type_2 or None),
        facility=Facility(
            id=str(loc.id),
            name=loc.name,
            address=loc.address_text,
            city=loc.city,
            state=loc.state,
            postal_code=loc.postal_code,
        ),
    )


class JheOrmDirectory(Directory):
    def __init__(self, vendors: Mapping[str, SourceSettings]):
        self.vendors = vendors

    def search(self, q, state=None, postal=None, limit=25):
        # JHE's picker uses its own /api/v1/ehr-patient-portal/brands; this is for the module's callers.
        qs = EhrBrandLocation.objects.select_related("brand__vendor")
        for word in (q or "").split():
            qs = qs.filter(Q(name__icontains=word) | Q(brand__name__icontains=word) | Q(city__icontains=word))
        if state:
            qs = qs.filter(state__iexact=state)
        if postal:
            qs = qs.filter(postal_code__startswith=postal)
        return [_selection(loc) for loc in qs.order_by("name")[:limit]]

    def selection(self, selection_id):
        try:
            return _selection(EhrBrandLocation.objects.select_related("brand__vendor").get(id=int(selection_id)))
        except (EhrBrandLocation.DoesNotExist, ValueError, TypeError):
            raise KeyError(selection_id) from None

    def settings_for(self, endpoint):
        base = self.vendors[endpoint.vendor]  # KeyError: JHE vendor without a PATIENT_ACCESS["VENDORS"] entry
        if base.registration.client_id:
            return base
        # The settings entry names no client id: use the one JHE keeps on the EhrVendor row (the
        # same Epic app registration the EHR Patient Portal uses).
        row = EhrVendor.objects.filter(name=endpoint.vendor).first()
        if row is None or not row.ehr_client_id:
            raise KeyError(endpoint.vendor)
        return replace(base, registration=replace(base.registration, client_id=row.ehr_client_id))

    def upsert_brands_bundle(self, bundle, vendor):
        # JHE's importer picks the vendor itself (Epic Sandbox / Epic Production), so `vendor` is unused.
        counts = import_brands_bundle(bundle)
        return ImportCounts(brands=counts["brands"], locations=counts["locations"])
