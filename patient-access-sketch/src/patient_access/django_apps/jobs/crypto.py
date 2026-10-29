"""Credentials at rest: Fernet encrypts EHR tokens, PKCE verifiers, participant secrets and spool
keys before they reach the database.

`ENCRYPTION_KEYS` is a list so keys can rotate: the first key encrypts, any key decrypts
(MultiFernet). To rotate, prepend a new key, keep the old one until in-flight attempts and jobs
have drained, then drop it. Settled jobs keep nothing encrypted.
"""

import json
from functools import cache

from cryptography.fernet import Fernet, MultiFernet

from patient_access.core.models import Secrets
from patient_access.core.source_settings import ConfigError

from .conf import patient_access_settings


@cache
def _fernet() -> MultiFernet:
    keys = patient_access_settings.ENCRYPTION_KEYS
    if isinstance(keys, str) or not keys:
        raise ConfigError("PATIENT_ACCESS['ENCRYPTION_KEYS'] must be a non-empty list of Fernet keys")
    try:
        return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])
    except (TypeError, ValueError) as e:
        raise ConfigError(f"PATIENT_ACCESS['ENCRYPTION_KEYS']: {e}") from e


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def encrypt_secrets(secrets: Secrets) -> str:
    return encrypt_str(json.dumps(secrets.reveal())) if secrets else ""


def decrypt_secrets(token: str) -> Secrets:
    return Secrets(json.loads(decrypt_str(token))) if token else Secrets()
