"""Registering a patient's EHR FhirSource: shared by the FhirSource API and in-process callers
(the Patient Access Module's sink delivers without an HTTP hop)."""

from django.db.models import Q

from core.models import EhrBrand, FhirSource


def resolve_brand(location, base_url):
    """The EhrBrand a registration is for: the picked facility's brand, else the one served at ``base_url``.

    ``base_url`` is matched with and without a trailing slash, since ``iss`` comes from the launch rather than the brands table and the two may differ only in that.
    """
    if location is not None:
        return location.brand
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        return None
    return EhrBrand.objects.filter(Q(fhir_base_url=base_url) | Q(fhir_base_url=base_url + "/")).order_by("id").first()


def get_or_create_ehr_source(patient, data_source, ehr_brand_location=None, base_url="", label=""):
    """Register a source, or return the patient's existing one for the same EHR brand: ``(source, created)``.

    A patient has one source per EHR brand and data source, so reconnecting does not stack a second copy of every record under a fresh source -- aux rows are only unique within a source. The brand comes from the picked facility, or from ``base_url`` (the SMART ``iss``) when none was picked. A new EHR source with no label is labelled "<vendor name> - <brand name>". A source that resolves to no brand is not an EHR connection and always creates.
    """
    brand = resolve_brand(ehr_brand_location, base_url)
    if brand is not None and not label:
        # Patient-facing, so it names the vendor and brand rather than anything technical.
        label = " - ".join(filter(None, [brand.vendor.name, brand.name]))
    if brand is not None:
        existing = (
            FhirSource.objects.filter(patient=patient, data_source=data_source, ehr_brand_location__brand=brand)
            .order_by("id")
            .first()
        )
        if existing is not None:
            return existing, False
    source = FhirSource.objects.create(
        patient=patient, data_source=data_source, label=label, ehr_brand_location=ehr_brand_location
    )
    return source, True
