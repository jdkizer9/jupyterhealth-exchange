"""Client authentication: what a registration may use, and the two plugs that hold the material
(design §10.9).

A ClientRegistration lists AuthMethods in preference order. Each names what it needs by
reference: a `kid` in the deployment's SigningKeys, or a secret name in its SecretStore. Rows
(attempts, jobs) record only the method name and kid used; the material never leaves the plugs.
`sign` takes the claims and returns the token, so the private key never leaves SigningKeys either
(a KMS implementation can sign without exporting it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

PUBLIC = "public"
CLIENT_SECRET_BASIC = "client_secret_basic"
CLIENT_SECRET_POST = "client_secret_post"
PRIVATE_KEY_JWT = "private_key_jwt"
METHODS = (PUBLIC, CLIENT_SECRET_BASIC, CLIENT_SECRET_POST, PRIVATE_KEY_JWT)


class CredentialError(Exception):
    """A method names material the deployment does not have. Safe to log, not to show patients."""


class SigningKeys(Protocol):
    def kids(self) -> set[str]: ...

    def public_jwks(self) -> dict:
        """{"keys": [...]}: the public halves, served by the host at /.well-known/jwks.json."""
        ...

    def sign(self, kid: str, claims: dict, alg: str) -> str:
        """A compact JWT over `claims`, header {alg, kid, typ}. Raises CredentialError for an unknown kid."""
        ...


class SecretStore(Protocol):
    def has(self, name: str) -> bool: ...

    def get(self, name: str) -> str:
        """Raises CredentialError when missing."""
        ...


@dataclass(frozen=True)
class AuthMethod:
    """One way to authenticate at the token endpoint. `kid`/`alg` for private_key_jwt, `secret`
    (a SecretStore name) for the client_secret methods, nothing for public."""

    method: str
    kid: str | None = None
    alg: str = "RS384"
    secret: str | None = None

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"auth method {self.method!r} not one of {METHODS}")
        if self.method == PRIVATE_KEY_JWT and not self.kid:
            raise ValueError("private_key_jwt needs a kid")
        if self.method in (CLIENT_SECRET_BASIC, CLIENT_SECRET_POST) and not self.secret:
            raise ValueError(f"{self.method} needs a secret name")

    def usable(self, keys: SigningKeys, secrets: SecretStore) -> bool:
        if self.method == PRIVATE_KEY_JWT:
            return self.kid in keys.kids()
        if self.method in (CLIENT_SECRET_BASIC, CLIENT_SECRET_POST):
            return secrets.has(self.secret or "")
        return True


@dataclass(frozen=True)
class Keyring:
    """The deployment's signing keys and secrets, passed together to the flow."""

    keys: SigningKeys
    secrets: SecretStore

    def choose(self, methods: tuple[AuthMethod, ...]) -> AuthMethod:
        """The first method this deployment can satisfy."""
        for m in methods:
            if m.usable(self.keys, self.secrets):
                return m
        raise CredentialError(f"none of {[m.method for m in methods]} is usable with this deployment's keys/secrets")


class NoSigningKeys:
    """For deployments with public clients only."""

    def kids(self) -> set[str]:
        return set()

    def public_jwks(self) -> dict:
        return {"keys": []}

    def sign(self, kid: str, claims: dict, alg: str) -> str:
        raise CredentialError(f"no signing key {kid!r}: this deployment has none")


class NoSecrets:
    def has(self, name: str) -> bool:
        return False

    def get(self, name: str) -> str:
        raise CredentialError(f"no secret {name!r}: this deployment has none")


PUBLIC_ONLY = Keyring(NoSigningKeys(), NoSecrets())
