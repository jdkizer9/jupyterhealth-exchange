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


def create_pull_fhir_source(patient, data_source, ehr_brand_location=None, base_url="", when=None):
    """One FhirSource per pull (Patient Access, design §14 / partner PRD §5.5): the pull's records
    live under their own source, and its id names the pull in the study's bucket
    (`{patient_id}/{fhir_source_id}`). Never reused across pulls, unlike get_or_create_ehr_source."""
    brand = resolve_brand(ehr_brand_location, base_url)
    label = " - ".join(filter(None, [brand.vendor.name if brand else None, brand.name if brand else None]))
    if when is not None:
        label = f"{label} - {when:%Y-%m-%d %H:%M}" if label else f"Pull {when:%Y-%m-%d %H:%M}"
    return FhirSource.objects.create(
        patient=patient, data_source=data_source, label=label or "EHR pull", ehr_brand_location=ehr_brand_location
    )
