"""SourceSettings: how we authenticate with, and pull from, one endpoint (design §10.8, §10.9).

    SourceSettings
      .registration  ClientRegistration   our app as registered with the vendor (phase 3)
      .fetch         FetchProfile         the query table and limits (phase 4)

A registration is one object per vendor registration ("Epic Sandbox" and "Epic Production" are
two); it carries nothing per endpoint, in particular no base URL (that is `Endpoint.iss`). A
fetch profile starts from a named profile in code (`profiles.py`) and may be overridden per
vendor, per customer and per endpoint by the directory implementation, which is the only place
the layering is visible.

`load_vendors` reads the `vendors` section of a deployment config (the standalone host). A
registration names its credentials (a signing key id, a secret name); the material lives in the
deployment's SigningKeys and SecretStore (`credentials.py`), never in settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .credentials import PUBLIC, AuthMethod


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
class ClientRegistration:
    """Our app as registered with one vendor (and environment)."""

    vendor: str
    client_id: str
    # Preference order; the launch uses the first the deployment can satisfy (Keyring.choose).
    methods: tuple[AuthMethod, ...] = (AuthMethod(PUBLIC),)
    base_scopes: tuple[str, ...] = ("launch/patient", "openid", "fhirUser")
    scope_version: str = "v1"  # "v2" turns patient/X.read into patient/X.rs

    def scopes(self, resource_scopes: list[str]) -> list[str]:
        """Base scopes plus the given resource scopes, in this registration's scope form."""
        out: list[str] = list(self.base_scopes)
        for s in resource_scopes:
            s = to_v2(s) if self.scope_version == "v2" else s
            if s not in out:
                out.append(s)
        return out


@dataclass(frozen=True)
class FetchProfile:
    """What to pull and how hard to try. Named profiles live in code; overrides narrow them."""

    name: str
    queries: tuple[QuerySpec, ...]
    page_cap: int = 200
    page_size: int | None = None  # adds _count when set
    timeout_seconds: float = 30.0
    max_retries: int = 3
    documents: DocumentPolicy = field(default_factory=DocumentPolicy)
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSettings:
    registration: ClientRegistration
    fetch: FetchProfile

    def scopes(self, resource_scopes: list[str] | None = None) -> list[str]:
        """Base scopes plus the resource scopes the query table needs (or the subset asked for)."""
        wanted = resource_scopes if resource_scopes is not None else [q.scope for q in self.fetch.queries]
        return self.registration.scopes(wanted)

    def queries_for(self, granted: list[str]) -> list[QuerySpec]:
        """Rows whose scope was granted, in either scope form (a server may echo scopes back converted).
        A granted wildcard (patient/*.read, patient/*.rs) covers all."""
        granted_set = set(granted)
        if {"patient/*.read", "patient/*.rs", "patient/*.*"} & granted_set:
            return list(self.fetch.queries)
        return [q for q in self.fetch.queries if q.scope in granted_set or to_v2(q.scope) in granted_set]


def to_v2(scope: str) -> str:
    return scope[: -len(".read")] + ".rs" if scope.endswith(".read") else scope


# --- overrides ---------------------------------------------------------------------------------

_LIMIT_KEYS = ("page_cap", "page_size", "timeout_seconds", "max_retries")


def apply_overrides(profile: FetchProfile, overrides: Mapping[str, Any] | None) -> FetchProfile:
    """One override format for every layer (vendor, customer, endpoint):

    {"override": {<label>: {<QuerySpec field>: value}}, "drop": [<label>], "add": [<row>],
     "page_cap": .., "page_size": .., "timeout_seconds": .., "max_retries": .., "documents": {..}}
    """
    if not overrides:
        return profile
    rows = list(profile.queries)
    by_label = {q.label for q in rows}
    unknown = set(overrides.get("override") or {}) - by_label
    if unknown:
        raise ConfigError(f"profile {profile.name!r}: override names unknown rows {sorted(unknown)}")
    drop = set(overrides.get("drop") or [])
    rows = [replace(q, **(overrides.get("override") or {}).get(q.label, {})) for q in rows if q.label not in drop]
    rows += [QuerySpec(**row) for row in overrides.get("add") or []]
    limits = {k: overrides[k] for k in _LIMIT_KEYS if k in overrides}
    docs = profile.documents
    if "documents" in overrides:
        raw = dict(overrides["documents"])
        for k in ("prefer", "sources"):
            if k in raw:
                raw[k] = tuple(raw[k])
        docs = replace(docs, **raw)
    return replace(profile, queries=tuple(rows), documents=docs, **limits)


# --- the standalone host's config file -------------------------------------------------------------


def load_vendors(
    config: Mapping[str, Any],
    profiles: Mapping[str, FetchProfile] | None = None,
) -> dict[str, SourceSettings]:
    """Build SourceSettings from the `vendors` section of a deployment config.

    Each entry: client_id, `auth_methods` (preference order, each {"method", "kid"?, "alg"?,
    "secret"?}; default public), base_scopes, scope_version, `profile` (a name in `profiles`,
    default "uscdi") and `overrides` (apply_overrides format). Credentials are named, not given.
    """
    from .profiles import PROFILES  # local import: profiles is data, settings is the vocabulary

    profiles = PROFILES if profiles is None else profiles
    out: dict[str, SourceSettings] = {}
    for name, raw in (config.get("vendors") or {}).items():
        raw = dict(raw)
        profile_name = raw.pop("profile", "uscdi")
        if profile_name not in profiles:
            raise ConfigError(f"vendor {name}: unknown profile {profile_name!r} (have {sorted(profiles)})")
        try:
            methods = tuple(AuthMethod(**m) for m in raw.pop("auth_methods", [{"method": PUBLIC}]))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"vendor {name}: {e}") from e
        if not methods:
            raise ConfigError(f"vendor {name}: auth_methods is empty")
        registration = ClientRegistration(
            vendor=name,
            client_id=raw.pop("client_id", ""),  # empty: the directory fills it in (JHE: from EhrVendor)
            methods=methods,
            base_scopes=tuple(raw.pop("base_scopes", ClientRegistration.base_scopes)),
            scope_version=raw.pop("scope_version", "v1"),
        )
        overrides = dict(raw.pop("overrides", None) or {})
        # Limits and documents given at the top level of the entry are overrides too.
        for k in (*_LIMIT_KEYS, "documents"):
            if k in raw:
                overrides[k] = raw.pop(k)
        if raw:
            raise ConfigError(f"vendor {name}: unknown keys {sorted(raw)}")
        out[name] = SourceSettings(registration=registration, fetch=apply_overrides(profiles[profile_name], overrides))
    return out
