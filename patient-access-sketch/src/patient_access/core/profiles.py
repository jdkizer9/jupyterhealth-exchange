"""Fetch profiles in code (design §10.8): the default USCDI query table and the per-vendor
variations we know about. A deployment picks a profile per vendor registration and narrows it
with overrides; it does not carry query tables in its config.
"""

from __future__ import annotations

from dataclasses import replace

from .source_settings import FetchProfile, QuerySpec, apply_overrides

# USCDI v3 for a patient-facing pull: one row per type (two for Observation, by category).
USCDI: tuple[QuerySpec, ...] = (
    QuerySpec("Demographics", "Patient", "Patient", "patient/Patient.read", single=True),
    QuerySpec("Conditions", "Condition", "Condition", "patient/Condition.read"),
    QuerySpec("Medications", "MedicationRequest", "MedicationRequest", "patient/MedicationRequest.read"),
    QuerySpec("Medication Dispenses", "MedicationDispense", "MedicationDispense", "patient/MedicationDispense.read"),
    QuerySpec("Allergies", "AllergyIntolerance", "AllergyIntolerance", "patient/AllergyIntolerance.read"),
    QuerySpec("Immunizations", "Immunization", "Immunization", "patient/Immunization.read"),
    QuerySpec("Procedures", "Procedure", "Procedure", "patient/Procedure.read"),
    QuerySpec("Labs", "Observation", "Observation?category=laboratory", "patient/Observation.read"),
    QuerySpec("Vital Signs", "Observation", "Observation?category=vital-signs", "patient/Observation.read"),
    QuerySpec(
        "Diagnostic Reports", "DiagnosticReport", "DiagnosticReport", "patient/DiagnosticReport.read", documents=True
    ),
    QuerySpec(
        "Documents",
        "DocumentReference",
        "DocumentReference?category=clinical-note",
        "patient/DocumentReference.read",
        documents=True,
    ),
    QuerySpec("Encounters", "Encounter", "Encounter", "patient/Encounter.read"),
    QuerySpec("Care Plans", "CarePlan", "CarePlan?category=assess-plan", "patient/CarePlan.read"),
    QuerySpec("Care Teams", "CareTeam", "CareTeam?status=active", "patient/CareTeam.read"),
    QuerySpec("Goals", "Goal", "Goal", "patient/Goal.read"),
    QuerySpec("Service Requests", "ServiceRequest", "ServiceRequest", "patient/ServiceRequest.read"),
    QuerySpec("Devices", "Device", "Device", "patient/Device.read", explicit_patient=True),
    QuerySpec(
        "Questionnaire Responses",
        "QuestionnaireResponse",
        "QuestionnaireResponse",
        "patient/QuestionnaireResponse.read",
    ),
)

_USCDI = FetchProfile("uscdi", USCDI)


def _derive(name: str, base: FetchProfile, overrides: dict) -> FetchProfile:
    return replace(apply_overrides(base, overrides), name=name)


PROFILES: dict[str, FetchProfile] = {
    "uscdi": _USCDI,
    # Epic: the USCDI table as published; site differences go in customer overrides.
    "epic": _derive("epic", _USCDI, {"page_cap": 200}),
    # The SMART reference server leaves DocumentReference.category empty; Epic's clinical-note
    # filter would hide every note there.
    "smart-reference": _derive("smart-reference", _USCDI, {"override": {"Documents": {"query": "DocumentReference"}}}),
}


def fetch_profile(name: str) -> FetchProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise KeyError(f"unknown fetch profile {name!r} (have {sorted(PROFILES)})") from None
