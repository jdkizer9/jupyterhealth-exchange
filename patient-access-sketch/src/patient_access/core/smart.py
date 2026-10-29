"""Phase 3: the SMART App Launch standalone flow, server side (design §3.2, §10.9, §10.10).

Discovery and PKCE state are adapted from jupyterhealth/jupyter-smart-on-fhir auth.py (BSD-3). The
server holds state, verifier and tokens; the browser only ever sees the authorize redirect.

Client authentication: the registration lists methods in preference order; the launch takes the
first the deployment's Keyring can satisfy and records it (method and kid) on the attempt. The
callback looks the registration up again and authenticates with the recorded method, signing
through SigningKeys and reading secrets from the SecretStore. Discovery is a check, not the
decider (Epic advertises every method at every customer): a method the server does not list is
logged and tried anyway.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests

from .credentials import (
    CLIENT_SECRET_BASIC,
    CLIENT_SECRET_POST,
    PRIVATE_KEY_JWT,
    PUBLIC,
    AuthMethod,
    CredentialError,
    Keyring,
)
from .interfaces import AuthAttemptStore
from .models import AuthAttempt, Participant, Selection, TokenResponse
from .source_settings import ClientRegistration, SourceSettings

log = logging.getLogger(__name__)

ATTEMPT_TTL = timedelta(minutes=10)
ASSERTION_TTL_SECONDS = 300  # SMART Backend Services: exp no more than 5 minutes out
DISCOVERY_TTL_SECONDS = 24 * 3600


class SmartError(Exception):
    """Phase 3 failed. The message is safe to show the patient."""


@dataclass(frozen=True)
class SmartConfiguration:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    raw: Mapping

    @property
    def capabilities(self) -> set[str]:
        return set(self.raw.get("capabilities") or [])

    @property
    def pkce_s256(self) -> bool:
        return "S256" in (self.raw.get("code_challenge_methods_supported") or [])

    @property
    def auth_methods(self) -> set[str]:
        return set(self.raw.get("token_endpoint_auth_methods_supported") or [])

    @property
    def signing_algs(self) -> set[str]:
        return set(self.raw.get("token_endpoint_auth_signing_alg_values_supported") or [])


_cache: dict[str, tuple[float, SmartConfiguration]] = {}
_cache_lock = threading.Lock()


def discover(iss: str, http: requests.Session, timeout: float = 15, use_cache: bool = True) -> SmartConfiguration:
    """`.well-known/smart-configuration`, cached per base URL for a day (a stale entry costs one
    failed launch). Endpoints are required; everything else is read as a check."""
    key = iss.rstrip("/")
    now = time.monotonic()
    if use_cache:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < DISCOVERY_TTL_SECONDS:
            return hit[1]
    # Strip a trailing slash: some servers (MedPlum) send iss with one and 401 the double-slash URL.
    url = f"{key}/.well-known/smart-configuration"
    try:
        r = http.get(url, headers={"Accept": "application/json"}, timeout=timeout)
        r.raise_for_status()
        raw = r.json()
        config = SmartConfiguration(iss, raw["authorization_endpoint"], raw["token_endpoint"], raw)
    except (requests.RequestException, ValueError, KeyError) as e:
        raise SmartError(f"Could not reach this organization's sign-in service ({type(e).__name__}).") from e
    with _cache_lock:
        _cache[key] = (now, config)
    return config


def clear_discovery_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def choose_method(registration: ClientRegistration, keyring: Keyring, config: SmartConfiguration) -> AuthMethod:
    """The first method we can satisfy; checked against what the server advertises."""
    try:
        method = keyring.choose(registration.methods)
    except CredentialError as e:
        log.error("vendor %s: %s", registration.vendor, e)
        raise SmartError("This service is not set up to connect to this organization yet.") from e
    if config.auth_methods and method.method != PUBLIC and method.method not in config.auth_methods:
        log.warning(
            "%s does not list %s (lists %s); trying it anyway",
            config.issuer,
            method.method,
            sorted(config.auth_methods),
        )
    if method.method == PRIVATE_KEY_JWT and config.signing_algs and method.alg not in config.signing_algs:
        log.error("%s accepts %s for client assertions, not %s", config.issuer, sorted(config.signing_algs), method.alg)
        raise SmartError("This organization does not accept this service's sign-in key.")
    return method


def begin(
    *,
    participant: Participant,
    selection: Selection,
    settings: SourceSettings,
    keyring: Keyring,
    redirect_uri: str,
    destination: str,
    attempts: AuthAttemptStore,
    http: requests.Session,
    clock: Callable[[], datetime],
    scopes: list[str] | None = None,
    scopes_declined: list[str] | None = None,
) -> str:
    """Discover, record the attempt, return the authorize URL to redirect the browser to."""
    registration = settings.registration
    iss = selection.endpoint.iss
    config = discover(iss, http, settings.fetch.timeout_seconds)
    method = choose_method(registration, keyring, config)
    if not config.pkce_s256:
        # SMART requires servers to accept PKCE; send it anyway and note the omission.
        log.info("%s does not list S256 in code_challenge_methods_supported; sending PKCE anyway", iss)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    scope_list = settings.scopes(scopes)
    now = clock()
    attempts.create(
        AuthAttempt(
            state=state,
            code_verifier=verifier,
            participant=participant,
            selection=selection,
            scopes=scope_list,
            redirect_uri=redirect_uri,
            token_endpoint=config.token_endpoint,
            destination=destination,
            created_at=now,
            expires_at=now + ATTEMPT_TTL,
            scopes_declined=list(scopes_declined or []),
            auth_method=method.method,
            key_id=method.kid,
            scope_version=registration.scope_version,
            pkce_advertised=config.pkce_s256,
        )
    )
    params = {
        "response_type": "code",
        "client_id": registration.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scope_list),  # `scope`, not `scopes` (jupyter-smart-on-fhir hub_service.py bug)
        "state": state,
        "aud": iss,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{config.authorization_endpoint}?{urlencode(params)}"


def assertion_claims(client_id: str, token_endpoint: str, now: int | None = None) -> dict:
    """private_key_jwt per SMART: iss = sub = client id, aud = token endpoint, unique jti, exp <= 5 min."""
    now = int(time.time()) if now is None else now
    return {
        "iss": client_id,
        "sub": client_id,
        "aud": token_endpoint,
        "jti": secrets.token_urlsafe(24),  # jupyter-smart-on-fhir sends the constant "jwt_id"
        "iat": now,
        "exp": now + ASSERTION_TTL_SECONDS,  # it sends 3600
    }


def _recorded_method(attempt: AuthAttempt, registration: ClientRegistration, keyring: Keyring) -> AuthMethod:
    """The method chosen at launch, as the (possibly reloaded) registration now describes it."""
    for m in registration.methods:
        if m.method == attempt.auth_method and m.kid == attempt.key_id:
            return m
    # The registration changed between launch and callback: choose again rather than fail.
    log.warning(
        "vendor %s: method %s/%s no longer configured; choosing again",
        registration.vendor,
        attempt.auth_method,
        attempt.key_id,
    )
    return keyring.choose(registration.methods)


def exchange_code(
    attempt: AuthAttempt,
    code: str,
    settings: SourceSettings,
    keyring: Keyring,
    http: requests.Session,
    clock: Callable[[], datetime],
) -> TokenResponse:
    """POST the code with the PKCE verifier and the client authentication chosen at launch."""
    registration = settings.registration
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": attempt.redirect_uri,
        "code_verifier": attempt.code_verifier,
    }
    auth = None
    try:
        method = _recorded_method(attempt, registration, keyring)
        if method.method == PUBLIC:
            data["client_id"] = registration.client_id
        elif method.method == CLIENT_SECRET_BASIC:
            auth = (registration.client_id, keyring.secrets.get(method.secret or ""))
        elif method.method == CLIENT_SECRET_POST:
            data.update(client_id=registration.client_id, client_secret=keyring.secrets.get(method.secret or ""))
        elif method.method == PRIVATE_KEY_JWT:
            claims = assertion_claims(registration.client_id, attempt.token_endpoint)
            data.update(
                client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                client_assertion=keyring.keys.sign(method.kid or "", claims, method.alg),
            )
    except CredentialError as e:
        log.error("vendor %s: %s", registration.vendor, e)
        raise SmartError("This service is not set up to connect to this organization yet.") from e
    try:
        r = http.post(
            attempt.token_endpoint,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=settings.fetch.timeout_seconds,
        )
    except requests.RequestException as e:
        raise SmartError(f"The organization's sign-in service did not respond ({type(e).__name__}).") from e
    if r.status_code != 200:
        detail = ""
        try:
            body = r.json()
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            pass
        raise SmartError(f"The organization refused the sign-in ({r.status_code} {detail}).".replace(" )", ")"))
    body = r.json()
    if not body.get("access_token") or not body.get("patient"):
        raise SmartError("The organization did not return patient access.")
    granted = (body.get("scope") or " ".join(attempt.scopes)).split()
    expires_in = int(body.get("expires_in") or 3600)
    # Refresh and id tokens are dropped here: we do not request offline access yet, and a token we
    # do not use must not be stored or logged (design §13 item 7).
    raw = {k: v for k, v in body.items() if k not in ("access_token", "refresh_token", "id_token")}
    return TokenResponse(
        access_token=body["access_token"],
        expires_at=clock() + timedelta(seconds=expires_in),
        scopes_granted=granted,
        patient=str(body["patient"]),
        token_type=body.get("token_type", "Bearer"),
        raw=raw,
        refresh_token_issued="refresh_token" in body,
    )
