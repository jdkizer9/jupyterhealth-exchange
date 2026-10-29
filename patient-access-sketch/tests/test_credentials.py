"""Client authentication end to end against a fake EHR: method choice, the four token-endpoint
styles, what is recorded (never the credential), refresh tokens dropped, discovery as a check."""

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

from patient_access.core.credentials import (
    CLIENT_SECRET_BASIC,
    CLIENT_SECRET_POST,
    PRIVATE_KEY_JWT,
    PUBLIC,
    PUBLIC_ONLY,
    AuthMethod,
    CredentialError,
    Keyring,
    NoSecrets,
)
from patient_access.core.flow import FlowError, finish_upstream_auth, start_upstream_auth
from patient_access.core.models import Participant
from patient_access.core.smart import clear_discovery_cache
from patient_access.credentials.env import EnvSecretStore, EnvSigningKeys
from patient_access.testing.memory import (
    DictSecretStore,
    InMemoryAuthAttemptStore,
    InMemoryDirectory,
    InMemoryJobStore,
    StaticIdentity,
    selection,
    settings,
)

KEYS = Path(__file__).resolve().parent.parent / "compose" / "dev-keys"
ISS = "https://ehr.example/fhir"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def signing_keys():
    return EnvSigningKeys([{"kid": "k1", "alg": "RS384", "path": str(KEYS / "smart-client-dev.pem")}])


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeEhr:
    """Discovery plus a token endpoint that records exactly what it was sent."""

    def __init__(self, methods=None, algs=None, refresh=False):
        self.discovery = {
            "authorization_endpoint": "https://ehr.example/authorize",
            "token_endpoint": "https://ehr.example/token",
            "code_challenge_methods_supported": ["S256"],
        }
        if methods is not None:
            self.discovery["token_endpoint_auth_methods_supported"] = methods
        if algs is not None:
            self.discovery["token_endpoint_auth_signing_alg_values_supported"] = algs
        self.refresh = refresh
        self.gets = 0
        self.token_request = None

    def get(self, url, headers=None, timeout=None):
        self.gets += 1
        return Resp(200, self.discovery)

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        self.token_request = {"data": dict(data), "auth": auth}
        body = {"access_token": "AT", "patient": "pat-1", "scope": "patient/Goal.read", "expires_in": 3600}
        if self.refresh:
            body["refresh_token"] = "RT-SECRET"
        return Resp(200, body)


@pytest.fixture(autouse=True)
def _fresh_discovery():
    clear_discovery_cache()
    yield
    clear_discovery_cache()


def run_flow(methods, keyring, ehr):
    sel = selection("s1", iss=ISS)
    directory = InMemoryDirectory([sel], {"v": settings("v", client_id="cid", methods=tuple(methods))})
    attempts, jobs = InMemoryAuthAttemptStore(clock=lambda: NOW), InMemoryJobStore()
    url = start_upstream_auth(
        participant=Participant("p1", "static"),
        selection_id="s1",
        directory=directory,
        keyring=keyring,
        attempts=attempts,
        redirect_uri="https://pa.example/callback",
        destination="d",
        http=ehr,
        clock=lambda: NOW,
        scopes=["patient/Goal.read"],
        scopes_declined=["patient/Device.read"],
    )
    state = parse_qs(urlparse(url).query)["state"][0]
    attempt = attempts.attempts[state]
    job_id = finish_upstream_auth(
        state=state,
        code="CODE",
        error=None,
        error_description=None,
        directory=directory,
        keyring=keyring,
        attempts=attempts,
        identity=StaticIdentity(),
        jobs=jobs,
        dispatcher=type("D", (), {"dispatch": lambda self, j: None})(),
        http=ehr,
        clock=lambda: NOW,
    )
    return attempt, jobs.get(job_id)


def test_public_sends_client_id_only():
    ehr = FakeEhr()
    attempt, job = run_flow([AuthMethod(PUBLIC)], PUBLIC_ONLY, ehr)
    assert ehr.token_request["data"]["client_id"] == "cid" and ehr.token_request["auth"] is None
    assert "client_assertion" not in ehr.token_request["data"]
    assert (attempt.auth_method, attempt.key_id) == (PUBLIC, None)
    assert job.source.auth_method == PUBLIC and job.source.scopes_declined == ["patient/Device.read"]


def test_private_key_jwt_signs_through_the_keyring():
    ehr = FakeEhr(methods=["private_key_jwt", "client_secret_basic"], algs=["RS384"])
    keyring = Keyring(signing_keys(), NoSecrets())
    attempt, job = run_flow([AuthMethod(PRIVATE_KEY_JWT, kid="k1")], keyring, ehr)
    data = ehr.token_request["data"]
    assert data["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    pub = (KEYS / "smart-client-dev.pub.pem").read_text()
    claims = jwt.decode(data["client_assertion"], pub, algorithms=["RS384"], audience="https://ehr.example/token")
    assert claims["iss"] == claims["sub"] == "cid"
    assert (job.source.auth_method, job.source.key_id) == (PRIVATE_KEY_JWT, "k1")


@pytest.mark.parametrize("style", [CLIENT_SECRET_BASIC, CLIENT_SECRET_POST])
def test_client_secret_styles(style):
    ehr = FakeEhr()
    keyring = Keyring(signing_keys(), DictSecretStore({"sandbox": "s3cret"}))
    run_flow([AuthMethod(style, secret="sandbox")], keyring, ehr)
    if style == CLIENT_SECRET_BASIC:
        assert ehr.token_request["auth"] == ("cid", "s3cret") and "client_secret" not in ehr.token_request["data"]
    else:
        assert ehr.token_request["data"]["client_secret"] == "s3cret" and ehr.token_request["auth"] is None


def test_first_usable_method_wins_and_unusable_ones_are_skipped():
    ehr = FakeEhr()
    # No key k2 and no secret "missing": falls through to public.
    methods = [
        AuthMethod(PRIVATE_KEY_JWT, kid="k2"),
        AuthMethod(CLIENT_SECRET_BASIC, secret="missing"),
        AuthMethod(PUBLIC),
    ]
    attempt, _ = run_flow(methods, Keyring(signing_keys(), DictSecretStore()), ehr)
    assert attempt.auth_method == PUBLIC


def test_nothing_usable_fails_the_launch_without_leaking_detail():
    with pytest.raises(FlowError, match="not set up to connect"):
        run_flow([AuthMethod(PRIVATE_KEY_JWT, kid="k1")], PUBLIC_ONLY, FakeEhr())


def test_unadvertised_method_is_tried_anyway_but_wrong_alg_fails():
    # Epic advertises everything; a server that omits our method still gets a try.
    run_flow([AuthMethod(PRIVATE_KEY_JWT, kid="k1")], Keyring(signing_keys(), NoSecrets()), FakeEhr(methods=["none"]))
    clear_discovery_cache()  # a different server at the same base URL
    with pytest.raises(FlowError, match="sign-in key"):
        run_flow([AuthMethod(PRIVATE_KEY_JWT, kid="k1")], Keyring(signing_keys(), NoSecrets()), FakeEhr(algs=["ES384"]))


def test_refresh_token_is_never_kept():
    ehr = FakeEhr(refresh=True)
    attempt, job = run_flow([AuthMethod(PUBLIC)], PUBLIC_ONLY, ehr)
    assert "RT-SECRET" not in repr(job) and "RT-SECRET" not in repr(job.credentials.reveal())
    assert "RT-SECRET" not in repr(attempt)


def test_discovery_is_cached_per_base_url():
    ehr = FakeEhr()
    run_flow([AuthMethod(PUBLIC)], PUBLIC_ONLY, ehr)
    run_flow([AuthMethod(PUBLIC)], PUBLIC_ONLY, ehr)
    assert ehr.gets == 1


def test_jwks_publishes_public_halves_only():
    jwks = signing_keys().public_jwks()
    (key,) = jwks["keys"]
    assert key["kid"] == "k1" and key["alg"] == "RS384" and key["use"] == "sig" and key["kty"] == "RSA"
    assert "d" not in key and "p" not in key  # no private parameters
    pub = (KEYS / "smart-client-dev.pub.pem").read_text()
    token = signing_keys().sign("k1", {"x": 1}, "RS384")
    assert jwt.decode(token, pub, algorithms=["RS384"]) == {"x": 1}
    assert jwt.decode(token, jwt.PyJWK(key).key, algorithms=["RS384"]) == {"x": 1}


def test_sign_refuses_unknown_kid_and_wrong_alg():
    with pytest.raises(CredentialError):
        signing_keys().sign("nope", {}, "RS384")
    with pytest.raises(CredentialError):
        signing_keys().sign("k1", {}, "ES384")


def test_env_secret_store_names_and_prefix():
    store = EnvSecretStore(
        names={"sandbox": "MY_SANDBOX_SECRET"}, environ={"MY_SANDBOX_SECRET": "a", "PA_SECRET_EPIC_PROD": "b"}
    )
    assert store.get("sandbox") == "a" and store.get("epic-prod") == "b" and not store.has("other")
    with pytest.raises(CredentialError):
        store.get("other")


def test_env_signing_keys_from_one_env_var():
    pem = (KEYS / "smart-client-dev.pem").read_text()
    keys = EnvSigningKeys(env="PA_SIGNING_KEYS", environ={"PA_SIGNING_KEYS": json.dumps([{"kid": "a", "pem": pem}])})
    assert keys.kids() == {"a"}


def test_auth_method_validation():
    with pytest.raises(ValueError):
        AuthMethod(PRIVATE_KEY_JWT)  # no kid
    with pytest.raises(ValueError):
        AuthMethod(CLIENT_SECRET_POST)  # no secret name
    with pytest.raises(ValueError):
        AuthMethod("magic")
