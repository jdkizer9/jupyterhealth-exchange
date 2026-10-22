"""Phase 3: the SMART App Launch standalone flow, server side (design §3.2).

Discovery and PKCE state are adapted from jupyterhealth/jupyter-smart-on-fhir auth.py (BSD-3). The
server holds state, verifier and tokens; the browser only ever sees the authorize redirect.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

import jwt
import requests

from .interfaces import AuthAttemptStore
from .models import AuthAttempt, Location, Participant, TokenResponse
from .vendors import VendorConfig

ATTEMPT_TTL = timedelta(minutes=10)
ASSERTION_TTL_SECONDS = 300  # SMART Backend Services: exp no more than 5 minutes out


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


def discover(iss: str, http: requests.Session, timeout: float = 15) -> SmartConfiguration:
    # Strip a trailing slash: some servers (MedPlum) send iss with one and 401 the double-slash URL.
    url = f"{iss.rstrip('/')}/.well-known/smart-configuration"
    try:
        r = http.get(url, headers={"Accept": "application/json"}, timeout=timeout)
        r.raise_for_status()
        raw = r.json()
        return SmartConfiguration(iss, raw["authorization_endpoint"], raw["token_endpoint"], raw)
    except (requests.RequestException, ValueError, KeyError) as e:
        raise SmartError(f"Could not reach this organization's sign-in service ({type(e).__name__}).") from e


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def begin(
    *,
    participant: Participant,
    location: Location,
    vendor: VendorConfig,
    redirect_uri: str,
    destination: str,
    attempts: AuthAttemptStore,
    http: requests.Session,
    clock: Callable[[], datetime],
    scopes: list[str] | None = None,
) -> str:
    """Discover, record the attempt, return the authorize URL to redirect the browser to."""
    config = discover(location.iss, http, vendor.timeout_seconds)
    if vendor.auth_method == "private_key_jwt" and config.auth_methods and "private_key_jwt" not in config.auth_methods:
        raise SmartError("This organization does not support the sign-in method this service is registered with.")
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    scope_list = vendor.scopes(scopes)
    now = clock()
    attempts.create(
        AuthAttempt(
            state=state,
            code_verifier=verifier,
            participant=participant,
            location_id=location.id,
            vendor=vendor.name,
            iss=location.iss,
            scopes=scope_list,
            redirect_uri=redirect_uri,
            token_endpoint=config.token_endpoint,
            destination=destination,
            created_at=now,
            expires_at=now + ATTEMPT_TTL,
        )
    )
    params = {
        "response_type": "code",
        "client_id": vendor.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scope_list),  # `scope`, not `scopes` (jupyter-smart-on-fhir hub_service.py bug)
        "state": state,
        "aud": location.iss,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{config.authorization_endpoint}?{urlencode(params)}"


def client_assertion(vendor: VendorConfig, token_endpoint: str, now: int | None = None) -> str:
    """private_key_jwt per SMART Backend Services: unique jti, exp <= 5 min, vendor's algorithm."""
    now = int(time.time()) if now is None else now
    claims = {
        "iss": vendor.client_id,
        "sub": vendor.client_id,
        "aud": token_endpoint,
        "jti": secrets.token_urlsafe(24),  # jupyter-smart-on-fhir sends the constant "jwt_id"
        "iat": now,
        "exp": now + ASSERTION_TTL_SECONDS,  # it sends 3600
    }
    if not vendor.private_key_pem:
        raise SmartError(f"vendor {vendor.name} has no private key for private_key_jwt")
    headers = {"kid": vendor.key_id} if vendor.key_id else None
    return jwt.encode(claims, vendor.private_key_pem, algorithm=vendor.jwt_alg, headers=headers)


def exchange_code(
    attempt: AuthAttempt, code: str, vendor: VendorConfig, http: requests.Session, clock: Callable[[], datetime]
) -> TokenResponse:
    """POST the code (with the PKCE verifier and the vendor's client authentication)."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": attempt.redirect_uri,
        "code_verifier": attempt.code_verifier,
    }
    auth = None
    if vendor.auth_method == "public":
        data["client_id"] = vendor.client_id
    elif vendor.auth_method == "client_secret_basic":
        auth = (vendor.client_id, vendor.client_secret or "")
    elif vendor.auth_method == "client_secret_post":
        data.update(client_id=vendor.client_id, client_secret=vendor.client_secret or "")
    elif vendor.auth_method == "private_key_jwt":
        data.update(
            client_assertion_type="urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            client_assertion=client_assertion(vendor, attempt.token_endpoint),
        )
    try:
        r = http.post(
            attempt.token_endpoint,
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=vendor.timeout_seconds,
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
    raw = {k: v for k, v in body.items() if k not in ("access_token", "refresh_token", "id_token")}
    return TokenResponse(
        access_token=body["access_token"],
        expires_at=clock() + timedelta(seconds=expires_in),
        scopes_granted=granted,
        patient=str(body["patient"]),
        token_type=body.get("token_type", "Bearer"),
        raw=raw,
    )
