"""OrmDirectory: the Directory plug over this app's tables."""

from __future__ import annotations

from django.db import transaction
from django.db.models import Q

from patient_access.core import models as m
from patient_access.core.interfaces import Directory

from .models import EhrBrandLocation, EhrVendor


def _location(loc: EhrBrandLocation) -> m.Location:
    return m.Location(
        id=str(loc.id),
        name=loc.name,
        address=loc.address_text,
        city=loc.city,
        state=loc.state,
        postal_code=loc.postal_code,
        brand_id=str(loc.brand_id),
        brand_name=loc.brand.name,
        iss=loc.brand.fhir_base_url,
        brand_npi=loc.brand.npi_type_2 or None,
        vendor=loc.brand.vendor.name,
    )


class OrmDirectory(Directory):
    def search(self, q: str, state: str | None = None, postal: str | None = None, limit: int = 25) -> list[m.Location]:
        qs = EhrBrandLocation.objects.select_related("brand", "brand__vendor")
        for word in (q or "").split():
            qs = qs.filter(Q(name__icontains=word) | Q(brand__name__icontains=word) | Q(city__icontains=word))
        if state:
            qs = qs.filter(state__iexact=state)
        if postal:
            qs = qs.filter(postal_code__startswith=postal)
        return [_location(loc) for loc in qs.order_by("name")[:limit]]

    def location(self, location_id: str) -> m.Location:
        try:
            return _location(EhrBrandLocation.objects.select_related("brand", "brand__vendor").get(id=int(location_id)))
        except (EhrBrandLocation.DoesNotExist, ValueError):
            raise KeyError(location_id) from None

    @transaction.atomic
    def upsert_brands_bundle(self, bundle: dict, vendor: str) -> m.ImportCounts:
        from .brands import import_brands_bundle

        return import_brands_bundle(bundle, EhrVendor.objects.get_or_create(name=vendor)[0])
