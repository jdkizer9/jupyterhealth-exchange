"""Builds the module from `patient_access_settings` (the project's PATIENT_ACCESS dict, see conf.py).

Each plug is {"class": "dotted.path", "options": {...}}. An option that is itself a plug spec is
built first, which is how a plug receives a store from an optional app (JwtDeepLinkIdentity's
`deeplink_store`) without this app importing it. A constructor parameter not given in options is
filled from `services` by name when one matches (`run`, `claimer`, `vendors`, ...).

The directory is a plug too, so a host with its own (JHE) names it here and never installs the
module's directory app.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

import requests
from django.utils.module_loading import import_string

from patient_access.core.credentials import Keyring, NoSecrets, NoSigningKeys
from patient_access.core.http import session as http_session
from patient_access.core.interfaces import (
    AuthAttemptStore,
    Directory,
    IdentityProvider,
    JobDispatcher,
    JobStore,
    Spool,
)
from patient_access.core.models import ContentPolicy
from patient_access.core.pipeline import Deps, Destination, run_job, utcnow
from patient_access.core.puller import fetch
from patient_access.core.source_settings import ConfigError, SourceSettings, load_vendors

from .conf import patient_access_settings
from .stores import OrmAuthAttemptStore, OrmJobClaimer, OrmJobStore


def _close_db_connection() -> None:
    """Django opens one DB connection per thread; a dispatcher thread must close its own."""
    from django.db import connection

    connection.close()


def build(spec: dict, services: dict[str, Any]) -> Any:
    cls = import_string(spec["class"])
    options = dict(spec.get("options") or {})
    for name, value in options.items():
        if isinstance(value, dict) and "class" in value:
            options[name] = build(value, services)
    for name in inspect.signature(cls).parameters:
        if name not in options and name in services:
            options[name] = services[name]
    return cls(**options)


class _Unset(JobDispatcher):
    def dispatch(self, job_id: str) -> None:
        raise RuntimeError("dispatcher not wired")


@dataclass
class Module:
    """Everything a host needs, built once per process."""

    identity: IdentityProvider
    directory: Directory
    auth_attempts: AuthAttemptStore
    jobs: JobStore
    spool: Spool
    destinations: dict[str, Destination]
    default_destinations: tuple[str, ...]  # what every launch records; one list per deployment
    dispatcher: JobDispatcher
    vendors: dict[str, SourceSettings]
    keyring: Keyring  # signing keys and secrets; the host serves keyring.keys.public_jwks()
    deps: Deps
    claimer: OrmJobClaimer
    site_url: str
    redirect_uri: str
    http: requests.Session
    clock: Callable[[], datetime] = field(default=utcnow)

    def run(self, job_id: str):
        return run_job(job_id, self.deps)


def _content(raw: dict | None) -> ContentPolicy:
    raw = raw or {}
    return ContentPolicy(
        documents=bool(raw.get("documents", True)),
        resource_types=frozenset(raw["resource_types"]) if raw.get("resource_types") else None,
        exclude_types=frozenset(raw.get("exclude_types") or ()),
    )


@cache
def get_module() -> Module:
    conf = patient_access_settings
    services: dict[str, Any] = {"site_url": conf.SITE_URL}
    vendors = load_vendors({"vendors": conf.VENDORS})
    services["vendors"] = vendors
    # Optional plugs: a public-client-only deployment names neither.
    keyring = Keyring(
        keys=build(conf.SIGNING_KEYS, services) if conf.SIGNING_KEYS else NoSigningKeys(),
        secrets=build(conf.SECRET_STORE, services) if conf.SECRET_STORE else NoSecrets(),
    )
    destinations = {
        name: Destination(
            name=name,
            formatter=build(d["formatter"], services),
            sink=build(d["sink"], services),
            required=bool(d.get("required", True)),
            content=_content(d.get("content")),
        )
        for name, d in conf.DESTINATIONS.items()
    }
    default_destinations = tuple(conf.DEFAULT_DESTINATIONS)
    unknown = set(default_destinations) - set(destinations)
    if unknown:
        raise ConfigError(f"PATIENT_ACCESS: DEFAULT_DESTINATIONS names unconfigured destinations {sorted(unknown)}")
    jobs = OrmJobStore()
    spool = build(conf.SPOOL, services)
    directory = build(conf.DIRECTORY, services)
    max_attempts = int(conf.MAX_ATTEMPTS)
    deps = Deps(
        jobs=jobs, spool=spool, destinations=destinations, directory=directory, fetch=fetch, max_attempts=max_attempts
    )
    module = Module(
        identity=build(conf.IDENTITY, services),
        directory=directory,
        auth_attempts=OrmAuthAttemptStore(),
        jobs=jobs,
        spool=spool,
        destinations=destinations,
        default_destinations=default_destinations,
        dispatcher=_Unset(),  # replaced below: a dispatcher may need the module's `run`, which needs the module
        vendors=vendors,
        keyring=keyring,
        deps=deps,
        claimer=OrmJobClaimer(max_attempts=max_attempts),
        site_url=conf.SITE_URL,
        # Where the EHR sends the patient back; a host with a registered path of its own sets it.
        redirect_uri=conf.REDIRECT_URI or f"{conf.SITE_URL}/callback",
        http=http_session(),
    )
    services["run"] = module.run
    services["claimer"] = module.claimer
    services["on_thread_exit"] = _close_db_connection
    module.dispatcher = build(conf.DISPATCHER, services)
    return module
