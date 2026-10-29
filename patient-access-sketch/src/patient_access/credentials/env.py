"""Credential plugs backed by the environment (or files, for dev): the default deployment.

    EnvSigningKeys(env="PA_SIGNING_KEYS")          JSON list [{kid, alg, pem}] in one env var
    EnvSigningKeys(keys=[{kid, alg, path}])        PEM files (dev keys checked into compose/)
    EnvSecretStore(names={"smart-sandbox": "PA_SMART_SANDBOX_CLIENT_SECRET"}, prefix="PA_SECRET_")

A secret name not listed in `names` maps to `prefix + NAME_IN_UPPER_SNAKE`.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

import jwt

from patient_access.core.credentials import CredentialError, SecretStore, SigningKeys


class EnvSigningKeys(SigningKeys):
    def __init__(
        self,
        keys: list[Mapping[str, str]] | None = None,
        env: str | None = None,
        environ: Mapping[str, str] | None = None,
    ):
        environ = os.environ if environ is None else environ
        entries: list[Mapping[str, str]] = list(keys or [])
        if env:
            raw = environ.get(env)
            if not raw:
                raise CredentialError(f"signing keys: env var {env} is not set")
            entries += json.loads(raw)
        self._keys: dict[str, tuple[str, str]] = {}  # kid -> (alg, pem)
        for e in entries:
            pem = e.get("pem")
            if not pem and e.get("path"):
                with open(e["path"], encoding="utf-8") as f:
                    pem = f.read()
            if not pem:
                raise CredentialError(f"signing key {e.get('kid')!r} has neither pem nor path")
            self._keys[e["kid"]] = (e.get("alg", "RS384"), pem)

    def kids(self) -> set[str]:
        return set(self._keys)

    def public_jwks(self) -> dict:
        out: list[dict[str, Any]] = []
        for kid, (alg, pem) in sorted(self._keys.items()):
            algorithm = jwt.get_algorithm_by_name(alg)
            private = algorithm.prepare_key(pem)
            jwk = json.loads(algorithm.to_jwk(private.public_key()))
            out.append({**jwk, "kid": kid, "alg": alg, "use": "sig"})
        return {"keys": out}

    def sign(self, kid: str, claims: dict, alg: str) -> str:
        try:
            key_alg, pem = self._keys[kid]
        except KeyError:
            raise CredentialError(f"no signing key {kid!r}") from None
        if alg != key_alg:
            raise CredentialError(f"signing key {kid!r} is {key_alg}, asked for {alg}")
        return jwt.encode(claims, pem, algorithm=alg, headers={"kid": kid, "typ": "JWT"})


class EnvSecretStore(SecretStore):
    def __init__(
        self,
        names: Mapping[str, str] | None = None,
        prefix: str = "PA_SECRET_",
        environ: Mapping[str, str] | None = None,
    ):
        self.names = dict(names or {})
        self.prefix = prefix
        self.environ = os.environ if environ is None else environ

    def _var(self, name: str) -> str:
        return self.names.get(name) or self.prefix + re.sub(r"[^A-Za-z0-9]", "_", name).upper()

    def has(self, name: str) -> bool:
        return bool(self.environ.get(self._var(name)))

    def get(self, name: str) -> str:
        value = self.environ.get(self._var(name))
        if not value:
            raise CredentialError(f"secret {name!r}: env var {self._var(name)} is not set")
        return value
