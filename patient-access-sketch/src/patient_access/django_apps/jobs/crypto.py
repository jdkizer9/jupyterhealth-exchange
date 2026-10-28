"""Credentials at rest: one deployment Fernet key encrypts tokens, participant secrets and spool keys."""

import json
from functools import cache

from cryptography.fernet import Fernet
from django.conf import settings

from patient_access.core.models import Secrets


@cache
def _fernet() -> Fernet:
    return Fernet(settings.PATIENT_ACCESS_DEPLOYMENT_KEY.encode())


def encrypt_str(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def encrypt_secrets(secrets: Secrets) -> str:
    return encrypt_str(json.dumps(secrets.reveal())) if secrets else ""


def decrypt_secrets(token: str) -> Secrets:
    return Secrets(json.loads(decrypt_str(token))) if token else Secrets()
