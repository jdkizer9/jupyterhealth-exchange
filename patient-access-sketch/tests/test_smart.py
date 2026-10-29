"""SMART client pieces that don't need the network, and the settings vocabulary."""

from pathlib import Path

import jwt
import pytest

from patient_access.core.profiles import PROFILES, USCDI
from patient_access.core.smart import ASSERTION_TTL_SECONDS, assertion_claims
from patient_access.core.source_settings import ConfigError, QuerySpec, apply_overrides, load_vendors
from patient_access.credentials.env import EnvSigningKeys
from patient_access.testing.memory import InMemoryDirectory, selection, settings

KEYS = Path(__file__).resolve().parent.parent / "compose" / "dev-keys"


def test_client_assertion_has_unique_jti_short_exp_and_kid():
    keys = EnvSigningKeys([{"kid": "sketch-1", "alg": "RS384", "path": str(KEYS / "smart-client-dev.pem")}])
    a = keys.sign("sketch-1", assertion_claims("cid", "https://ehr/token", now=1000), "RS384")
    b = keys.sign("sketch-1", assertion_claims("cid", "https://ehr/token", now=1000), "RS384")
    pub = (KEYS / "smart-client-dev.pub.pem").read_text()
    ca = jwt.decode(a, pub, algorithms=["RS384"], audience="https://ehr/token", options={"verify_exp": False})
    cb = jwt.decode(b, pub, algorithms=["RS384"], audience="https://ehr/token", options={"verify_exp": False})
    assert ca["jti"] != cb["jti"]
    assert ca["exp"] - ca["iat"] == ASSERTION_TTL_SECONDS <= 300
    assert ca["iss"] == ca["sub"] == "cid"
    assert jwt.get_unverified_header(a) == {"alg": "RS384", "kid": "sketch-1", "typ": "JWT"}


TABLE = (
    QuerySpec("Labs", "Observation", "Observation?category=laboratory", "patient/Observation.read"),
    QuerySpec("Goals", "Goal", "Goal", "patient/Goal.read"),
)


def test_scopes_and_query_filtering_in_both_scope_forms():
    a = settings("a", TABLE, base_scopes=("launch/patient",))
    b = settings("b", TABLE, base_scopes=("launch/patient",), scope_version="v2")
    assert a.scopes() == ["launch/patient", "patient/Observation.read", "patient/Goal.read"]
    assert b.scopes() == ["launch/patient", "patient/Observation.rs", "patient/Goal.rs"]
    assert [q.label for q in a.queries_for(["patient/Goal.read"])] == ["Goals"]
    assert [q.label for q in a.queries_for(["patient/Goal.rs"])] == ["Goals"]  # echoed back converted
    assert [q.label for q in b.queries_for(["patient/*.rs"])] == ["Labs", "Goals"]


def test_profiles_in_code_and_overrides():
    assert PROFILES["uscdi"].queries == USCDI
    docs = next(q for q in PROFILES["smart-reference"].queries if q.label == "Documents")
    assert docs.query == "DocumentReference"  # the reference server has no clinical-note category
    narrowed = apply_overrides(
        PROFILES["uscdi"],
        {
            "drop": ["Devices"],
            "override": {"Labs": {"query": "Observation?category=laboratory&_count=50"}},
            "page_cap": 5,
        },
    )
    assert "Devices" not in {q.label for q in narrowed.queries} and narrowed.page_cap == 5
    assert next(q for q in narrowed.queries if q.label == "Labs").query.endswith("_count=50")
    with pytest.raises(ConfigError):
        apply_overrides(PROFILES["uscdi"], {"override": {"Nope": {}}})


def test_load_vendors_from_config_uses_profiles():
    vendors = load_vendors(
        {
            "vendors": {
                "a": {"client_id": "c", "profile": "smart-reference", "base_scopes": ["launch/patient"], "page_cap": 3},
                "b": {"client_id": "c", "scope_version": "v2", "overrides": {"drop": ["Goals"]}},
            }
        }
    )
    assert vendors["a"].fetch.page_cap == 3 and vendors["a"].fetch.name == "smart-reference"
    assert "Goals" not in {q.label for q in vendors["b"].fetch.queries}
    assert vendors["b"].scopes(["patient/Goal.read"]) == ["launch/patient", "openid", "fhirUser", "patient/Goal.rs"]
    with pytest.raises(ConfigError):
        load_vendors({"vendors": {"x": {"client_id": "c", "profile": "nope"}}})


def test_directory_merges_customer_overrides_into_settings():
    base = settings("v", TABLE, page_cap=100)
    d = InMemoryDirectory(
        selections=[selection("s1", customer_id="org-1"), selection("s2", customer_id="org-2")],
        settings={"v": base},
        overrides={"org-1": {"drop": ["Goals"], "page_cap": 2}},
    )
    s1 = d.settings_for(d.selection("s1").endpoint)
    s2 = d.settings_for(d.selection("s2").endpoint)
    assert [q.label for q in s1.fetch.queries] == ["Labs"] and s1.fetch.page_cap == 2
    assert [q.label for q in s2.fetch.queries] == ["Labs", "Goals"] and s2.fetch.page_cap == 100
    assert s1.registration is base.registration  # the registration is per vendor, untouched by overrides


def test_plan_outcomes_marks_not_requested_and_not_granted():
    from patient_access.core.flow import plan_outcomes

    s = settings("v", TABLE)
    outcomes = plan_outcomes(s, requested=["launch/patient", "patient/Goal.read"], granted=["patient/Goal.read"])
    assert {o.label: o.status for o in outcomes} == {"Labs": "not_requested", "Goals": "pending"}
    outcomes = plan_outcomes(
        s, requested=["patient/Observation.read", "patient/Goal.read"], granted=["patient/Goal.rs"]
    )
    assert {o.label: o.status for o in outcomes} == {"Labs": "not_granted", "Goals": "pending"}
