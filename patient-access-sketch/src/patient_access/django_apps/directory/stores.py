"""OrmDirectory: the Directory plug over this app's tables.

Selection ids: `f<facility pk>` for a facility, `b<brand pk>` for a brand without facilities.
Settings: the vendor's registration and profile come from the deployment config (`vendors`,
built by load_vendors); customer and endpoint overrides from their rows, merged here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from django.db import transaction
from django.db.models import Q

from patient_access.core import models as m
from patient_access.core.interfaces import Directory
from patient_access.core.source_settings import SourceSettings, apply_overrides

from .models import EhrBrand, EhrEndpoint, EhrFacility, EhrVendor


def _endpoint(ep: EhrEndpoint) -> m.Endpoint:
    return m.Endpoint(
        id=str(ep.id),
        iss=ep.address,
        vendor=ep.vendor.name,
        fhir_version=ep.fhir_version,
        customer_id=ep.customer.external_id if ep.customer else None,
        customer_name=(ep.customer.name or None) if ep.customer else None,
    )


def _brand(b: EhrBrand) -> m.Brand:
    return m.Brand(id=str(b.id), name=b.name, endpoint=_endpoint(b.endpoint), npi=b.npi_type_2 or None)


def _facility_selection(f: EhrFacility) -> m.Selection:
    return m.Selection(
        id=f"f{f.id}",
        brand=_brand(f.brand),
        facility=m.Facility(
            id=str(f.id), name=f.name, address=f.address_text, city=f.city, state=f.state, postal_code=f.postal_code
        ),
    )


_BRAND_RELATED = ("endpoint__vendor", "endpoint__customer")


class OrmDirectory(Directory):
    def __init__(self, vendors: Mapping[str, SourceSettings]):
        self.vendors = vendors

    def search(self, q: str, state: str | None = None, postal: str | None = None, limit: int = 25) -> list[m.Selection]:
        fac = EhrFacility.objects.select_related("brand", *[f"brand__{r}" for r in _BRAND_RELATED])
        brands = EhrBrand.objects.select_related(*_BRAND_RELATED).filter(facilities__isnull=True)
        for word in (q or "").split():
            fac = fac.filter(Q(name__icontains=word) | Q(brand__name__icontains=word) | Q(city__icontains=word))
            brands = brands.filter(name__icontains=word)
        if state:
            fac = fac.filter(state__iexact=state)
            brands = brands.none()
        if postal:
            fac = fac.filter(postal_code__startswith=postal)
            brands = brands.none()
        out = [_facility_selection(f) for f in fac.order_by("name")[:limit]]
        out += [m.Selection(id=f"b{b.id}", brand=_brand(b)) for b in brands.order_by("name")[:limit]]
        return sorted(out, key=lambda s: s.name)[:limit]

    def selection(self, selection_id: str) -> m.Selection:
        kind, pk = selection_id[:1], selection_id[1:]
        try:
            if kind == "f":
                f = EhrFacility.objects.select_related("brand", *[f"brand__{r}" for r in _BRAND_RELATED]).get(
                    id=int(pk)
                )
                return _facility_selection(f)
            if kind == "b":
                return m.Selection(
                    id=selection_id, brand=_brand(EhrBrand.objects.select_related(*_BRAND_RELATED).get(id=int(pk)))
                )
        except (EhrFacility.DoesNotExist, EhrBrand.DoesNotExist, ValueError):
            pass
        raise KeyError(selection_id)

    def settings_for(self, endpoint: m.Endpoint) -> SourceSettings:
        base = self.vendors[endpoint.vendor]  # KeyError: no registration for this vendor
        fetch = base.fetch
        row = EhrEndpoint.objects.select_related("customer").filter(address=endpoint.iss).first()
        if row is not None:
            if row.customer is not None:
                fetch = apply_overrides(fetch, row.customer.overrides)
            fetch = apply_overrides(fetch, row.overrides)
        return replace(base, fetch=fetch)

    @transaction.atomic
    def upsert_brands_bundle(self, bundle: dict, vendor: str) -> m.ImportCounts:
        from .brands import import_brands_bundle

        return import_brands_bundle(bundle, EhrVendor.objects.get_or_create(name=vendor)[0])
