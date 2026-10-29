"""The tables every Django host needs: auth attempts and jobs. Rows become core dataclasses in stores.py."""

import uuid

from django.db import models


def _uuid() -> str:
    return uuid.uuid4().hex


# --- phase 3 ----------------------------------------------------------------------------------


class AuthAttemptRecord(models.Model):
    state = models.CharField(primary_key=True)
    code_verifier_enc = models.TextField()
    participant = models.JSONField()  # ref, provider, attrs, handle
    participant_secrets_enc = models.TextField(blank=True, default="")
    selection = models.JSONField()  # Selection as a value: brand, endpoint, facility
    scopes = models.JSONField()
    scopes_declined = models.JSONField(default=list)
    auth = models.JSONField(default=dict)  # method, kid, scope_version, pkce_advertised: never a credential
    redirect_uri = models.TextField()
    token_endpoint = models.TextField()
    destinations = models.JSONField(default=list)
    created_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


# --- the job ----------------------------------------------------------------------------------


class JobRecord(models.Model):
    id = models.CharField(primary_key=True, default=_uuid)
    status = models.CharField(db_index=True)
    participant = models.JSONField()
    participant_secrets_enc = models.TextField(blank=True, default="")
    source = models.JSONField()
    destinations = models.JSONField(default=list)
    credentials_enc = models.TextField(blank=True, default="")  # EHR token, spool key
    token_expires_at = models.DateTimeField(null=True, blank=True)
    outcomes = models.JSONField(default=list)
    documents = models.JSONField(default=dict)
    spool_ref = models.CharField(null=True, blank=True)
    deliveries = models.JSONField(default=dict)  # destination name -> Delivery (status, attempts, receipt, ...)
    failure = models.TextField(null=True, blank=True)
    attempts = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # The polling dispatcher's lease; written only by OrmJobClaimer, never by OrmJobStore.save.
    lease_owner = models.CharField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "lease_expires_at"])]
