"""Builds the plugs from the deployment config (settings.PATIENT_ACCESS_CONFIG, a JSON file).

Each plug is {"class": "dotted.path", "options": {...}}. Strings may use ${ENV_VAR}. An option
that is itself a plug spec is built first, which is how a plug receives a store from an optional
app (JwtDeepLinkIdentity's `deeplink_store`) without this app importing it. A constructor
parameter not given in options is filled from `services` by name when one matches (`run`,
`claimer`, `vendors`, ...).

The directory is a plug too, so a host with its own (JHE) names it here and never installs the
module's directory app.
"""

from __future__ import annotations

import inspect
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from typing import Any

import requests
from django.conf import settings
from django.utils.module_loading import import_string

from patient_access.core.http import session as http_session
from patient_access.core.interfaces import (
    AuthAttemptStore,
    Directory,
    IdentityProvider,
    JobDispatcher,
    JobStore,
    Spool,
)
from patient_access.core.pipeline import Deps, Destination, run_job, utcnow
from patient_access.core.puller import fetch
from patient_access.core.vendors import VendorConfig, load_vendors

from .stores import OrmAuthAttemptStore, OrmJobClaimer, OrmJobStore

_VAR = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _close_db_connection() -> None:
    """Django opens one DB connection per thread; a dispatcher thread must close its own."""
    from django.db import connection

    connection.close()


def _subst(value: Any) -> Any:
    if isinstance(value, str):
        return _VAR.sub(lambda mt: os.environ[mt.group(1)], value)
    if isinstance(value, list):
        return [_subst(v) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v) for k, v in value.items()}
    return value


def build(spec: dict, services: dict[str, Any]) -> Any:
    cls = import_string(spec["class"])
    options = _subst(spec.get("options") or {})
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

    config: dict
    identity: IdentityProvider
    directory: Directory
    auth_attempts: AuthAttemptStore
    jobs: JobStore
    spool: Spool
    destinations: dict[str, Destination]
    default_destination: str
    dispatcher: JobDispatcher
    vendors: dict[str, VendorConfig]
    deps: Deps
    claimer: OrmJobClaimer
    site_url: str
    redirect_uri: str
    http: requests.Session
    clock: Callable[[], datetime] = field(default=utcnow)

    def run(self, job_id: str):
        return run_job(job_id, self.deps)


def load_config(path: str | None = None) -> dict:
    """A config may start with {"extends": "<path>"}: top-level keys then replace the base's."""
    with open(path or settings.PATIENT_ACCESS_CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    if base := config.pop("extends", None):
        return {**load_config(base), **config}
    return config


@cache
def get_module() -> Module:
    config = load_config()
    services: dict[str, Any] = {"site_url": settings.SITE_URL}
    vendors = load_vendors(_subst({"vendors": config.get("vendors"), "query_tables": config.get("query_tables")}))
    services["vendors"] = vendors
    destinations = {}
    for name, d in (config.get("destinations") or {}).items():
        destinations[name] = Destination(
            name=name, formatter=build(d["formatter"], services), sink=build(d["sink"], services)
        )
    jobs = OrmJobStore()
    spool = build(config["spool"], services)
    max_attempts = int(config.get("max_attempts", 3))
    deps = Deps(
        jobs=jobs, spool=spool, destinations=destinations, vendors=vendors, fetch=fetch, max_attempts=max_attempts
    )
    module = Module(
        config=config,
        identity=build(config["identity"], services),
        directory=build(config["directory"], services),
        auth_attempts=OrmAuthAttemptStore(),
        jobs=jobs,
        spool=spool,
        destinations=destinations,
        default_destination=_subst(config["default_destination"]),
        dispatcher=_Unset(),  # replaced below: InlineDispatcher needs the module's `run`, which needs the module
        vendors=vendors,
        deps=deps,
        claimer=OrmJobClaimer(max_attempts=max_attempts),
        site_url=settings.SITE_URL,
        # Where the EHR sends the patient back; a host with a registered path of its own sets it.
        redirect_uri=_subst(config.get("redirect_uri") or f"{settings.SITE_URL}/callback"),
        http=http_session(),
    )
    services["run"] = module.run
    services["claimer"] = module.claimer
    services["on_thread_exit"] = _close_db_connection
    module.dispatcher = build(config["dispatcher"], services)
    return module
