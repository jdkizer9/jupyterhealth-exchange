"""The module's settings, the way reusable Django apps do it: a `PATIENT_ACCESS` dict in the
project's settings.py, merged over `DEFAULTS` here and read lazily (so `override_settings`
works and nothing is read at import time).

    PATIENT_ACCESS = {
        "ENCRYPTION_KEYS": os.environ["PATIENT_ACCESS_ENCRYPTION_KEYS"].split(","),  # Fernet, at rest; first encrypts
        "IDENTITY":  {"class": "core.patient_access.identity.JheUserIdentity"},
        "DIRECTORY": {"class": "core.patient_access.directory.JheOrmDirectory"},
        "SPOOL":     {"class": "patient_access.spool.local_disk.LocalDiskSpool", "options": {"root": "/data/pa"}},
        "DISPATCHER": {"class": "patient_access.dispatch.thread.ThreadDispatcher"},
        "DESTINATIONS": {"s3": {"formatter": {...}, "sink": {...}, "required": True, "content": {"documents": True}}},
        "DEFAULT_DESTINATIONS": ["s3"],
        "REDIRECT_URI": "https://host/clients/uscdi-patient-access/callback",   # default: SITE_URL + /callback
        "VENDORS": {"Epic Sandbox": {"client_id": "...", "auth_methods": [{"method": "public"}], "profile": "epic"}},
        "SIGNING_KEYS": {"class": "patient_access.credentials.env.EnvSigningKeys", "options": {...}},  # optional
        "SECRET_STORE": {"class": "patient_access.credentials.env.EnvSecretStore"},                    # optional
        "MAX_ATTEMPTS": 3,
        "SEED": {...},   # dev only: what seed_directory / seed_patient_access_sandbox load
    }

A plug is {"class": "dotted.path", "options": {...}}; an option that is itself a plug spec is
built first. Values are plain Python, so a project reads its environment in settings.py as it
does for everything else; there is no substitution syntax here.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.signals import setting_changed

from patient_access.core.source_settings import ConfigError

DEFAULTS: dict[str, Any] = {
    "ENCRYPTION_KEYS": None,  # list of Fernet keys; see crypto.py for rotation
    "SITE_URL": None,  # falls back to settings.SITE_URL
    "IDENTITY": None,
    "DIRECTORY": None,
    "SPOOL": None,
    "DISPATCHER": {"class": "patient_access.dispatch.thread.ThreadDispatcher", "options": {"lease_seconds": 600}},
    "DESTINATIONS": {},
    "DEFAULT_DESTINATIONS": [],
    "REDIRECT_URI": None,
    "VENDORS": {},
    "SIGNING_KEYS": None,
    "SECRET_STORE": None,
    "MAX_ATTEMPTS": 3,
    "SEED": {},
}
REQUIRED = ("ENCRYPTION_KEYS", "IDENTITY", "DIRECTORY", "SPOOL")


class PatientAccessSettings:
    """`patient_access_settings.DIRECTORY` etc.: the project's value, else the default."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            user = getattr(settings, "PATIENT_ACCESS", None) or {}
            unknown = set(user) - set(DEFAULTS)
            if unknown:
                raise ConfigError(f"PATIENT_ACCESS: unknown keys {sorted(unknown)}")
            merged = {**DEFAULTS, **user}
            missing = [k for k in REQUIRED if not merged.get(k)]
            if missing:
                raise ConfigError(f"PATIENT_ACCESS: missing {missing}")
            if merged["SITE_URL"] is None:
                merged["SITE_URL"] = getattr(settings, "SITE_URL", "").rstrip("/")
            self._cache = merged
        return self._cache

    def __getattr__(self, name: str) -> Any:
        if name not in DEFAULTS:
            raise AttributeError(name)
        return self._load()[name]

    def reload(self) -> None:
        self._cache = None


patient_access_settings = PatientAccessSettings()


def _on_setting_changed(*, setting: str, **kwargs: Any) -> None:
    if setting in ("PATIENT_ACCESS", "SITE_URL"):
        patient_access_settings.reload()
        from .crypto import _fernet
        from .wiring import get_module

        get_module.cache_clear()
        _fernet.cache_clear()


setting_changed.connect(_on_setting_changed)
