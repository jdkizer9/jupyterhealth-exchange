"""The module's tables. Rows are translated to core dataclasses in stores.py; nothing else reads them."""

import uuid

from django.db import models


def _uuid() -> str:
    return uuid.uuid4().hex


# --- directory (moved from JHE's core/models/ehr_brand.py; same shape) ------------------------


class EhrVendor(models.Model):
    """A vendor key into the deployment's VendorConfig (client id, auth method, query table live there)."""

    name = models.CharField(unique=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class EhrBrand(models.Model):
    name = models.CharField()
    vendor = models.ForeignKey(EhrVendor, on_delete=models.PROTECT, related_name="brands")
    fhir_base_url = models.TextField(unique=True)  # the SMART iss
    npi_type_2 = models.CharField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class EhrBrandLocation(models.Model):
    brand = models.ForeignKey(EhrBrand, on_delete=models.CASCADE, related_name="locations")
    name = models.CharField()
    address_text = models.TextField(blank=True, default="")
    city = models.CharField(null=True, blank=True)
    state = models.CharField(null=True, blank=True)
    postal_code = models.CharField(null=True, blank=True)
    country = models.CharField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["state"]),
            models.Index(fields=["postal_code"]),
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return self.name


# --- identity plug storage --------------------------------------------------------------------


class ConsumedDeepLink(models.Model):
    """JwtDeepLinkIdentity's DeepLinkStore: a jti is inserted when its link creates a job."""

    jti = models.CharField(primary_key=True)
    ref = models.CharField()
    exp = models.DateTimeField()
    used_at = models.DateTimeField(auto_now_add=True)


# --- phase 3 ----------------------------------------------------------------------------------


class AuthAttemptRecord(models.Model):
    state = models.CharField(primary_key=True)
    code_verifier_enc = models.TextField()
    participant = models.JSONField()  # ref, provider, attrs, handle
    participant_secrets_enc = models.TextField(blank=True, default="")
    location_id = models.CharField()
    vendor = models.CharField()
    iss = models.TextField()
    scopes = models.JSONField()
    redirect_uri = models.TextField()
    token_endpoint = models.TextField()
    destination = models.CharField()
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
    destination = models.CharField()
    credentials_enc = models.TextField(blank=True, default="")  # EHR token, spool key
    token_expires_at = models.DateTimeField(null=True, blank=True)
    outcomes = models.JSONField(default=list)
    documents = models.JSONField(default=dict)
    spool_ref = models.CharField(null=True, blank=True)
    receipt = models.JSONField(null=True, blank=True)
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
