"""The module's Directory over JHE's EhrVendor / EhrBrand / EhrBrandLocation tables."""

from django.db.models import Q
from patient_access.core.interfaces import Directory
from patient_access.core.models import ImportCounts, Location

from core.management.commands.import_ehr_brands import import_brands_bundle
from core.models import EhrBrandLocation


def _location(loc):
    brand = loc.brand
    return Location(
        id=str(loc.id),
        name=loc.name,
        address=loc.address_text,
        city=loc.city,
        state=loc.state,
        postal_code=loc.postal_code,
        brand_id=str(brand.id),
        brand_name=brand.name,
        iss=brand.fhir_base_url,
        brand_npi=brand.npi_type_2 or None,
        # The module's vendor config is keyed by JHE's vendor names ("Epic Sandbox", "SMART Sandbox").
        vendor=brand.vendor.name,
    )


class JheOrmDirectory(Directory):
    def search(self, q, state=None, postal=None, limit=25):
        # JHE's picker uses its own /api/v1/ehr-patient-portal/brands; this is for the module's callers.
        qs = EhrBrandLocation.objects.select_related("brand__vendor")
        for word in (q or "").split():
            qs = qs.filter(Q(name__icontains=word) | Q(brand__name__icontains=word) | Q(city__icontains=word))
        if state:
            qs = qs.filter(state__iexact=state)
        if postal:
            qs = qs.filter(postal_code__startswith=postal)
        return [_location(loc) for loc in qs.order_by("name")[:limit]]

    def location(self, location_id):
        try:
            return _location(EhrBrandLocation.objects.select_related("brand__vendor").get(id=int(location_id)))
        except (EhrBrandLocation.DoesNotExist, ValueError, TypeError):
            raise KeyError(location_id) from None

    def upsert_brands_bundle(self, bundle, vendor):
        # JHE's importer picks the vendor itself (Epic Sandbox / Epic Production), so `vendor` is unused.
        counts = import_brands_bundle(bundle)
        return ImportCounts(brands=counts["brands"], locations=counts["locations"])
