"""The Patient Access Module's FhirSource for a pull (sketch; additive to the FhirSource API, which is
untouched). `resolve_brand` mirrors FhirSourceViewSet._resolve_brand so this module stands alone."""

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


def get_or_create_pull_fhir_source(patient, data_source, ehr_brand_location=None, base_url=""):
    """The FhirSource a pull delivers under: the patient's existing one for the same EHR brand and
    data source, else a new one (the FhirSource API's rule, so a reconnect does not stack a second
    copy of every record under a fresh source). Returns ``(source, created)``. Pulls under the same
    source are told apart in the bucket by the key's timestamp: `{patient_id}/{fhir_source_id}/{timestamp}`."""
    brand = resolve_brand(ehr_brand_location, base_url)
    if brand is not None:
        existing = (
            FhirSource.objects.filter(patient=patient, data_source=data_source, ehr_brand_location__brand=brand)
            .order_by("id")
            .first()
        )
        if existing is not None:
            return existing, False
    label = " - ".join(filter(None, [brand.vendor.name, brand.name])) if brand else "EHR pull"
    return FhirSource.objects.create(
        patient=patient, data_source=data_source, label=label, ehr_brand_location=ehr_brand_location
    ), True
