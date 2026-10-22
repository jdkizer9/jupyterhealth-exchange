"""VendorConfig: everything that differs per EHR vendor. Config, not a plug (design §7.2).

Loaded from the deployment's config file (JSON; stdlib only). Secrets are referenced by env var
name, never inlined: `client_secret_env`, `private_key_env` / `private_key_path`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

AUTH_METHODS = ("public", "client_secret_basic", "client_secret_post", "private_key_jwt")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class QuerySpec:
    """One row of the query table: what to GET and how to scope it to the patient."""

    label: str
    resource_type: str
    query: str  # "Observation?category=laboratory"; "Patient" with single=True is an instance read
    scope: str  # the patient/<Type>.read scope that gates it
    single: bool = False  # instance read of the patient's own id (Patient)
    explicit_patient: bool = False  # the search needs ?patient= spelled out (Device); otherwise added for us
    documents: bool = False  # this type's attachments feed the documents pass


@dataclass(frozen=True)
class DocumentPolicy:
    enabled: bool = True
    max_count: int = 200
    max_bytes_each: int = 5 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    # Preference order when one attachment is offered in several content types (smallest wins on ties).
    prefer: tuple[str, ...] = ("text/plain", "text/html", "application/pdf", "text/rtf", "application/xml")
    # "presentedForm" for DiagnosticReport renderings, "content" for DocumentReference
    sources: tuple[str, ...] = ("DocumentReference", "DiagnosticReport")


@dataclass(frozen=True)
class VendorConfig:
    name: str
    client_id: str
    auth_method: str
    base_scopes: tuple[str, ...]
    queries: tuple[QuerySpec, ...]
    client_secret: str | None = None
    private_key_pem: str | None = None
    key_id: str | None = None
    jwt_alg: str = "RS384"
    scope_version: str = "v1"  # "v2" turns patient/X.read into patient/X.rs
    page_cap: int = 200
    page_size: int | None = None  # adds _count when set
    timeout_seconds: float = 30.0
    max_retries: int = 3
    documents: DocumentPolicy = field(default_factory=DocumentPolicy)
    extra: Mapping[str, Any] = field(default_factory=dict)

    def scopes(self, resource_scopes: list[str] | None = None) -> list[str]:
        """Base scopes plus the resource scopes the query table needs (or the subset asked for)."""
        wanted = resource_scopes if resource_scopes is not None else [q.scope for q in self.queries]
        out: list[str] = list(self.base_scopes)
        for s in wanted:
            s = _to_v2(s) if self.scope_version == "v2" else s
            if s not in out:
                out.append(s)
        return out

    def queries_for(self, granted: list[str]) -> list[QuerySpec]:
        """Rows whose scope was granted. A granted wildcard (patient/*.read, patient/*.rs) covers all."""
        granted_set = set(granted)
        if {"patient/*.read", "patient/*.rs", "patient/*.*"} & granted_set:
            return list(self.queries)
        out = []
        for q in self.queries:
            s = _to_v2(q.scope) if self.scope_version == "v2" else q.scope
            if s in granted_set or q.scope in granted_set:
                out.append(q)
        return out


def _to_v2(scope: str) -> str:
    return scope[: -len(".read")] + ".rs" if scope.endswith(".read") else scope


def load_vendors(config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, VendorConfig]:
    """Build VendorConfigs from the `vendors` and `query_tables` sections of a deployment config."""
    environ = os.environ if environ is None else environ
    tables = {
        name: tuple(QuerySpec(**row) for row in rows) for name, rows in (config.get("query_tables") or {}).items()
    }
    vendors: dict[str, VendorConfig] = {}
    for name, raw in (config.get("vendors") or {}).items():
        raw = dict(raw)
        method = raw.pop("auth_method", "public")
        if method not in AUTH_METHODS:
            raise ConfigError(f"vendor {name}: auth_method {method!r} not one of {AUTH_METHODS}")
        table_name = raw.pop("queries")
        if table_name not in tables:
            raise ConfigError(f"vendor {name}: unknown query table {table_name!r}")
        secret = None
        if env := raw.pop("client_secret_env", None):
            secret = environ.get(env)
            if not secret:
                raise ConfigError(f"vendor {name}: env var {env} is not set")
        pem = None
        if env := raw.pop("private_key_env", None):
            pem = environ.get(env)
            if not pem:
                raise ConfigError(f"vendor {name}: env var {env} is not set")
        if path := raw.pop("private_key_path", None):
            with open(path, encoding="utf-8") as f:
                pem = f.read()
        if method.startswith("client_secret") and not secret:
            raise ConfigError(f"vendor {name}: {method} needs client_secret_env")
        if method == "private_key_jwt" and not pem:
            raise ConfigError(f"vendor {name}: private_key_jwt needs private_key_env or private_key_path")
        docs = raw.pop("documents", None)
        if docs is not None:
            docs = dict(docs)
            for k in ("prefer", "sources"):
                if k in docs:
                    docs[k] = tuple(docs[k])
        vendors[name] = VendorConfig(
            name=name,
            auth_method=method,
            queries=tables[table_name],
            client_secret=secret,
            private_key_pem=pem,
            base_scopes=tuple(raw.pop("base_scopes", ("launch/patient", "openid", "fhirUser"))),
            documents=DocumentPolicy(**docs) if docs is not None else DocumentPolicy(),
            client_id=raw.pop("client_id"),
            **{k: v for k, v in raw.items() if k in VendorConfig.__dataclass_fields__},
            extra={k: v for k, v in raw.items() if k not in VendorConfig.__dataclass_fields__},
        )
    return vendors
