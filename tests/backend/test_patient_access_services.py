"""The two services the Patient Access Module's in-process sink calls (scenario D sketch):
FhirSource registration and R4 import, each without an HTTP request."""

import pytest

from core.models import EhrBrand, EhrBrandLocation, EhrVendor, FhirAuxResource, FhirSource, JheUser
from core.services.fhir_import import import_r4_bundle

CONDITION = {
    "resourceType": "Condition",
    "id": "cond-1",
    "clinicalStatus": {
        "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": "active"}]
    },
    "code": {"text": "Hypertension"},
    "subject": {"reference": "Patient/p1"},
}


@pytest.fixture
def location(db):
    vendor = EhrVendor.objects.create(name="SMART Sandbox")
    brand = EhrBrand.objects.create(name="Sandbox Health", vendor=vendor, fhir_base_url="https://ehr.example/fhir")
    return EhrBrandLocation.objects.create(brand=brand, name="Sandbox Clinic")


def test_import_r4_bundle_in_process(patient, device):
    source = FhirSource.objects.create(patient=patient, data_source=device, label="EHR")
    bundle = {"resourceType": "Bundle", "type": "batch", "entry": [{"resource": CONDITION}, {"resource": "junk"}]}
    out = import_r4_bundle(patient.jhe_user, source, bundle)
    assert out["type"] == "batch-response"
    statuses = [e["response"]["status"] for e in out["entry"]]
    assert statuses[0] == "201 Created" and not statuses[1].startswith("2")
    assert FhirAuxResource.objects.filter(fhir_source=source, resource_type="Condition").count() == 1


def test_import_refuses_someone_elses_source(patient, device):
    source = FhirSource.objects.create(patient=patient, data_source=device, label="EHR")
    other = JheUser.objects.create_user(email="other-patient@example.org", password="x", user_type="patient")
    out = import_r4_bundle(other, source, {"resourceType": "Bundle", "entry": [{"resource": CONDITION}]})
    assert out["entry"][0]["response"]["status"].startswith("403")
    assert not FhirAuxResource.objects.filter(fhir_source=source).exists()


def test_pull_fhir_source_is_new_every_time(patient, device, location):
    from datetime import UTC, datetime

    from core.services.fhir_source import create_pull_fhir_source

    when = datetime(2026, 10, 29, 14, 5, tzinfo=UTC)
    a = create_pull_fhir_source(patient, device, ehr_brand_location=location, when=when)
    b = create_pull_fhir_source(patient, device, ehr_brand_location=location, when=when)
    assert a.id != b.id and a.ehr_brand_location_id == location.id
    assert a.label == "SMART Sandbox - Sandbox Health - 2026-10-29 14:05"
