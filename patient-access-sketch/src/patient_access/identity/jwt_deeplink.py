"""JwtDeepLinkIdentity: a signed, expiring, single-use link (design §2.1).

The contract is the JWT, not a call to the module: whoever holds an issuer's private key mints
links; the module holds only public keys, keyed by `iss`. Opening a link does not spend it;
`commit` (end of phase 3, before the Job row) does, by a unique insert into the DeepLinkStore.
"""

from __future__ import annotations

import re
import secrets
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import jwt

from patient_access.core.interfaces import IdentityError
from patient_access.core.models import InboundRequest, Participant

ALGORITHMS = ["ES256"]
AUDIENCE = "patient-access"
QUERY_PARAM = "t"
_RESERVED = {"iss", "sub", "aud", "exp", "iat", "nbf", "jti", "brand", "attrs"}


class DeepLinkStore(Protocol):
    """Owned by this plug; implemented by the host (Django model) or in memory for tests."""

    def is_consumed(self, jti: str) -> bool: ...

    def consume(self, jti: str, ref: str, exp: datetime) -> bool:
        """Insert; False if the jti was already there."""
        ...


class InMemoryDeepLinkStore:
    def __init__(self):
        self.used: dict[str, tuple[str, datetime]] = {}

    def is_consumed(self, jti: str) -> bool:
        return jti in self.used

    def consume(self, jti: str, ref: str, exp: datetime) -> bool:
        if jti in self.used:
            return False
        self.used[jti] = (ref, exp)
        return True


def _read(path_or_pem: str) -> str:
    return path_or_pem if path_or_pem.lstrip().startswith("-----BEGIN") else Path(path_or_pem).read_text()


class JwtDeepLinkIdentity:
    name = "jwt_deeplink"

    def __init__(
        self,
        issuers: Mapping[str, str],
        deeplink_store: DeepLinkStore,
        audience: str = AUDIENCE,
        leeway_seconds: int = 30,
        max_ttl_seconds: int = 30 * 24 * 3600,
        mint: Mapping[str, str] | None = None,
    ):
        """issuers: iss -> public key (PEM text or a path). mint: {"iss", "private_key"} for the dev
        command only; a production deployment omits it and the consumer mints."""
        self.keys = {iss: _read(k) for iss, k in issuers.items()}
        self.store = deeplink_store
        self.audience = audience
        self.leeway = leeway_seconds
        self.max_ttl = max_ttl_seconds
        self.mint_config = dict(mint or {})

    def resolve(self, request: InboundRequest) -> Participant:
        token = request.query.get(QUERY_PARAM)
        if not token:
            raise IdentityError("This page needs the personal link from your invitation.")
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            iss = unverified.get("iss")
            key = self.keys.get(iss)
            if key is None:
                raise IdentityError("This link was not issued by a recognized study.")
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=iss,
                leeway=self.leeway,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "jti"]},
            )
        except jwt.ExpiredSignatureError:
            raise IdentityError("This link has expired. Please ask the study team for a new one.") from None
        except jwt.PyJWTError as e:
            raise IdentityError(f"This link is not valid ({type(e).__name__}).") from None
        if claims["exp"] - claims["iat"] > self.max_ttl:
            raise IdentityError("This link's lifetime is longer than this deployment allows.")
        if self.store.is_consumed(claims["jti"]):
            raise IdentityError("This link has already been used.")
        attrs: dict[str, Any] = dict(claims.get("attrs") or {})
        attrs.update({"iss": iss, "link_exp": claims["exp"]})
        if claims.get("brand"):
            attrs["brand"] = str(claims["brand"])
        return Participant(ref=str(claims["sub"]), provider=self.name, attrs=attrs, handle=claims["jti"])

    def commit(self, participant: Participant) -> None:
        exp = datetime.fromtimestamp(int(participant.attrs["link_exp"]), UTC)
        if not self.store.consume(participant.handle, participant.ref, exp):
            raise IdentityError("This link has already been used.")


_TTL = re.compile(r"^(\d+)([smhd])$")


def parse_ttl(text: str) -> int:
    m = _TTL.match(text.strip())
    if not m:
        raise ValueError(f"ttl like 30m, 12h, 7d; got {text!r}")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def mint_link(
    site_url: str,
    private_key: str,
    iss: str,
    ref: str,
    ttl_seconds: int = 7 * 86400,
    brand: str | None = None,
    attrs: Mapping[str, Any] | None = None,
    audience: str = AUDIENCE,
    now: int | None = None,
) -> str:
    """What a consumer runs (or reimplements) to enrol someone. Writes nothing to the module."""
    now = int(time.time()) if now is None else now
    claims: dict[str, Any] = {
        "iss": iss,
        "sub": ref,
        "aud": audience,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": secrets.token_urlsafe(16),
    }
    if brand:
        claims["brand"] = brand
    if attrs:
        bad = _RESERVED & set(attrs)
        if bad:
            raise ValueError(f"attrs may not use reserved names {sorted(bad)}")
        claims["attrs"] = dict(attrs)
    token = jwt.encode(claims, _read(private_key), algorithm="ES256")
    return f"{site_url.rstrip('/')}/start?{urlencode({QUERY_PARAM: token})}"
