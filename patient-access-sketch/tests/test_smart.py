"""SMART client pieces that don't need the network."""

from pathlib import Path

import jwt

from patient_access.core.smart import ASSERTION_TTL_SECONDS, client_assertion
from patient_access.core.vendors import VendorConfig, load_vendors

KEYS = Path(__file__).resolve().parent.parent / "compose" / "dev-keys"


def _vendor():
    return VendorConfig(
        name="v",
        client_id="cid",
        auth_method="private_key_jwt",
        base_scopes=(),
        queries=(),
        private_key_pem=(KEYS / "smart-client-dev.pem").read_text(),
        key_id="sketch-1",
        jwt_alg="RS384",
    )


def test_client_assertion_has_unique_jti_short_exp_and_vendor_alg():
    v = _vendor()
    a, b = client_assertion(v, "https://ehr/token", now=1000), client_assertion(v, "https://ehr/token", now=1000)
    pub = (KEYS / "smart-client-dev.pub.pem").read_text()
    ca = jwt.decode(a, pub, algorithms=["RS384"], audience="https://ehr/token", options={"verify_exp": False})
    cb = jwt.decode(b, pub, algorithms=["RS384"], audience="https://ehr/token", options={"verify_exp": False})
    assert ca["jti"] != cb["jti"]
    assert ca["exp"] - ca["iat"] == ASSERTION_TTL_SECONDS <= 300
    assert ca["iss"] == ca["sub"] == "cid"
    assert jwt.get_unverified_header(a) == {"alg": "RS384", "kid": "sketch-1", "typ": "JWT"}


def test_scopes_and_query_filtering():
    vendors = load_vendors(
        {
            "query_tables": {
                "t": [
                    {
                        "label": "Labs",
                        "resource_type": "Observation",
                        "query": "Observation?category=laboratory",
                        "scope": "patient/Observation.read",
                    },
                    {"label": "Goals", "resource_type": "Goal", "query": "Goal", "scope": "patient/Goal.read"},
                ]
            },
            "vendors": {
                "a": {"client_id": "c", "queries": "t", "base_scopes": ["launch/patient"]},
                "b": {"client_id": "c", "queries": "t", "base_scopes": ["launch/patient"], "scope_version": "v2"},
            },
        }
    )
    assert vendors["a"].scopes() == ["launch/patient", "patient/Observation.read", "patient/Goal.read"]
    assert vendors["b"].scopes() == ["launch/patient", "patient/Observation.rs", "patient/Goal.rs"]
    assert [q.label for q in vendors["a"].queries_for(["patient/Goal.read"])] == ["Goals"]
    assert [q.label for q in vendors["b"].queries_for(["patient/*.rs"])] == ["Labs", "Goals"]
