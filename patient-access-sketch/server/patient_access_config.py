"""The standalone host's PATIENT_ACCESS settings, one dict per dev variant (PA_VARIANT).

Plain Python read by server/settings.py, the way a reusable app is configured. Every value is a
public local-only dev value or comes from the environment docker-compose.yml sets.
"""

from __future__ import annotations

import os
from typing import Any

KEYS = "/code/compose/dev-keys"
SANDBOX_CLIENT_ID = "patient-access-sketch"  # the SMART sandbox accepts any client id
SMART = {"base_scopes": ["launch/patient", "openid", "fhirUser"], "profile": "smart-reference"}


def _env(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError:
        raise RuntimeError(f"{name} must be set (see docker-compose.yml)") from None


def base() -> dict[str, Any]:
    out_dir, spool_dir, s3_endpoint = _env("PA_OUT_DIR"), _env("PA_SPOOL_DIR"), _env("PA_S3_ENDPOINT")
    s3 = {"endpoint_url": s3_endpoint, "access_key": _env("PA_S3_ACCESS_KEY"), "secret_key": _env("PA_S3_SECRET_KEY")}
    return {
        "ENCRYPTION_KEYS": _env("PA_ENCRYPTION_KEYS").split(","),
        "IDENTITY": {
            "class": "patient_access.identity.jwt_deeplink.JwtDeepLinkIdentity",
            "options": {
                "issuers": {"study-dev": f"{KEYS}/deeplink-study-dev.pub.pem"},
                "mint": {"iss": "study-dev", "private_key": f"{KEYS}/deeplink-study-dev.pem"},
                "deeplink_store": {"class": "patient_access.django_apps.deeplink.stores.OrmDeepLinkStore"},
            },
        },
        "DIRECTORY": {"class": "patient_access.django_apps.directory.stores.OrmDirectory"},
        "SPOOL": {"class": "patient_access.spool.local_disk.LocalDiskSpool", "options": {"root": spool_dir}},
        "DESTINATIONS": {
            "local": {
                "formatter": {"class": "patient_access.formatters.composition.CompositionBundleFormatter"},
                "sink": {
                    "class": "patient_access.destinations.local_dir.LocalDirectorySink",
                    "options": {"root": out_dir},
                },
            },
            "s3": {
                "formatter": {"class": "patient_access.formatters.composition.CompositionBundleFormatter"},
                "sink": {
                    "class": "patient_access.destinations.s3.S3Sink",
                    "options": {
                        "bucket": "patient-access",
                        "layout": "pulls/{participant.ref}/{job.id}",
                        "create_bucket": True,
                        **s3,
                    },
                },
            },
        },
        # "s3" (Silo) or "local" (./local/out).
        "DEFAULT_DESTINATIONS": [os.environ.get("PA_DESTINATION", "s3")],
        "DISPATCHER": {
            "class": "patient_access.dispatch.db_polling.DbPollingDispatcher",
            "options": {"interval_seconds": 2, "lease_seconds": 600, "sweep_every": 10, "spool_max_age_hours": 24},
        },
        "MAX_ATTEMPTS": 3,
        "SIGNING_KEYS": {
            "class": "patient_access.credentials.env.EnvSigningKeys",
            "options": {"keys": [{"kid": "sketch-1", "alg": "RS384", "path": f"{KEYS}/smart-client-dev.pem"}]},
        },
        "SECRET_STORE": {
            "class": "patient_access.credentials.env.EnvSecretStore",
            "options": {"names": {"smart-sandbox": "PA_SMART_SANDBOX_CLIENT_SECRET"}},
        },
        "VENDORS": {
            "smart-public": {"client_id": SANDBOX_CLIENT_ID, "auth_methods": [{"method": "public"}], **SMART},
            "smart-symmetric": {
                "client_id": SANDBOX_CLIENT_ID,
                "auth_methods": [{"method": "client_secret_basic", "secret": "smart-sandbox"}],
                **SMART,
            },
            "smart-asymmetric": {
                "client_id": SANDBOX_CLIENT_ID,
                "auth_methods": [{"method": "private_key_jwt", "kid": "sketch-1", "alg": "RS384"}],
                **SMART,
            },
        },
        "SEED": {
            "bundles": [{"file": "/code/compose/data/epic-brands.sample.json", "vendor": "epic"}],
            "smart_sandbox": _sandbox_brands(),
        },
    }


def _sandbox_brands() -> list[dict[str, Any]]:
    secret = os.environ.get("PA_SMART_SANDBOX_CLIENT_SECRET", "")
    asym = {
        "client_type": "confidential-asymmetric",
        "pkce": "auto",
        "jwks_public_key": f"{KEYS}/smart-client-dev.pub.pem",
        "kid": "sketch-1",
        "alg": "RS384",
    }
    sym = {"client_type": "confidential-symmetric", "pkce": "auto", "client_secret": secret}
    express = {"skip_login": True, "skip_auth": True}
    boston = {"line": "1 Sandbox Way", "city": "Boston", "state": "MA", "postal": "02115"}
    cambridge = {"line": "4 Sandbox Way", "city": "Cambridge", "state": "MA", "postal": "02139"}
    return [
        {
            "id": "smart-public",
            "name": "SMART Sandbox Health (public client)",
            "vendor": "smart-public",
            **boston,
            "launch": {"client_type": "public", "pkce": "always"},
        },
        {
            "id": "smart-symmetric",
            "name": "SMART Sandbox Health (confidential symmetric)",
            "vendor": "smart-symmetric",
            **boston,
            "launch": sym,
        },
        {
            "id": "smart-asymmetric",
            "name": "SMART Sandbox Health (confidential asymmetric)",
            "vendor": "smart-asymmetric",
            **boston,
            "launch": asym,
        },
        {
            "id": "smart-express",
            "name": "SMART Sandbox Express (scripted: no login, fixed patient)",
            "vendor": "smart-public",
            **cambridge,
            "launch": {
                "client_type": "public",
                "pkce": "always",
                **express,
                "patient": ["12edb5a0-ec71-45b7-aff6-0fef1c382881"],
            },
        },
        {
            "id": "smart-express-symmetric",
            "name": "SMART Sandbox Express symmetric (scripted)",
            "vendor": "smart-symmetric",
            **cambridge,
            "launch": {**sym, **express, "patient": ["80a75b5a-fd30-4f38-895d-d8098fe7206e"]},
        },
        {
            "id": "smart-express-asymmetric",
            "name": "SMART Sandbox Express asymmetric (scripted)",
            "vendor": "smart-asymmetric",
            **cambridge,
            "launch": {**asym, **express, "patient": ["80a75b5a-fd30-4f38-895d-d8098fe7206e"]},
        },
    ]


def patient_access_config(variant: str) -> dict[str, Any]:
    """`default`, or one of the variants the compose overlays select with PA_VARIANT."""
    cfg = base()
    if variant == "default":
        return cfg
    if variant == "s3spool":
        cfg["SPOOL"] = {
            "class": "patient_access.spool.s3_staging.S3StagingSpool",
            "options": {
                "bucket": "patient-access",
                "prefix": "staging",
                "endpoint_url": _env("PA_S3_ENDPOINT"),
                "access_key": _env("PA_S3_ACCESS_KEY"),
                "secret_key": _env("PA_S3_SECRET_KEY"),
                "chunk": 500,
                "expire_days": 1,
                "create_bucket": True,
            },
        }
    elif variant == "thread":
        cfg["DISPATCHER"] = {
            "class": "patient_access.dispatch.thread.ThreadDispatcher",
            "options": {"lease_seconds": 600},
        }
    elif variant == "dramatiq":
        cfg["DISPATCHER"] = {"class": "patient_access.dispatch.dramatiq_dispatcher.DramatiqDispatcher"}
    elif variant == "jhe":
        # Scenario B: deliver into the JHE dev stack over its HTTP API.
        cfg["IDENTITY"] = {
            "class": "patient_access.identity.jhe_invitation.JheInvitationIdentity",
            "options": {"base_url": _env("PA_JHE_URL"), "public_host": _env("PA_JHE_PUBLIC_HOST")},
        }
        cfg["DESTINATIONS"]["jhe"] = {
            "formatter": {"class": "patient_access.formatters.r4_batch.R4BatchFormatter", "options": {"chunk": 100}},
            "sink": {
                "class": "patient_access.destinations.jhe.JheHttpSink",
                "options": {"base_url": _env("PA_JHE_URL"), "data_source_id": _env("PA_JHE_DATA_SOURCE_ID")},
            },
        }
        cfg["DEFAULT_DESTINATIONS"] = ["jhe"]
    else:
        raise RuntimeError(f"unknown PA_VARIANT {variant!r} (default, s3spool, thread, dramatiq, jhe)")
    return cfg
