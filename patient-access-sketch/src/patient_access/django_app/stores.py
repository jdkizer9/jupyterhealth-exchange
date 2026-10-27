"""ORM implementations of the core Protocols, plus the plug-owned stores and the polling claimer.

The edge where rows become dataclasses (design §9 rule 2).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from patient_access.core import models as m
from patient_access.core.interfaces import AuthAttemptStore, Directory, JobStore
from patient_access.dispatch.db_polling import JobClaimer
from patient_access.identity.jwt_deeplink import DeepLinkStore

from .crypto import decrypt_secrets, decrypt_str, encrypt_secrets, encrypt_str
from .models import (
    AuthAttemptRecord,
    ConsumedDeepLink,
    EhrBrandLocation,
    EhrVendor,
    JobRecord,
)

# --- serde ------------------------------------------------------------------------------------


def participant_to_json(p: m.Participant) -> dict:
    return {"ref": p.ref, "provider": p.provider, "attrs": p.attrs, "handle": p.handle}


def participant_from_json(d: dict, secrets_enc: str) -> m.Participant:
    return m.Participant(
        ref=d["ref"],
        provider=d["provider"],
        attrs=d.get("attrs") or {},
        handle=d.get("handle") or "",
        secrets=decrypt_secrets(secrets_enc),
    )


def _receipt_from_json(d: dict | None) -> m.Receipt | None:
    return m.Receipt(**d) if d else None


# --- directory --------------------------------------------------------------------------------


def _location(loc: EhrBrandLocation) -> m.Location:
    return m.Location(
        id=str(loc.id),
        name=loc.name,
        address=loc.address_text,
        city=loc.city,
        state=loc.state,
        postal_code=loc.postal_code,
        brand_id=str(loc.brand_id),
        brand_name=loc.brand.name,
        iss=loc.brand.fhir_base_url,
        brand_npi=loc.brand.npi_type_2 or None,
        vendor=loc.brand.vendor.name,
    )


class OrmDirectory(Directory):
    def search(self, q: str, state: str | None = None, postal: str | None = None, limit: int = 25) -> list[m.Location]:
        qs = EhrBrandLocation.objects.select_related("brand", "brand__vendor")
        for word in (q or "").split():
            qs = qs.filter(Q(name__icontains=word) | Q(brand__name__icontains=word) | Q(city__icontains=word))
        if state:
            qs = qs.filter(state__iexact=state)
        if postal:
            qs = qs.filter(postal_code__startswith=postal)
        return [_location(loc) for loc in qs.order_by("name")[:limit]]

    def location(self, location_id: str) -> m.Location:
        try:
            return _location(EhrBrandLocation.objects.select_related("brand", "brand__vendor").get(id=int(location_id)))
        except (EhrBrandLocation.DoesNotExist, ValueError):
            raise KeyError(location_id) from None

    @transaction.atomic
    def upsert_brands_bundle(self, bundle: dict, vendor: str) -> m.ImportCounts:
        from .brands import import_brands_bundle

        return import_brands_bundle(bundle, EhrVendor.objects.get_or_create(name=vendor)[0])


# --- identity plug storage --------------------------------------------------------------------


class OrmDeepLinkStore(DeepLinkStore):
    def is_consumed(self, jti: str) -> bool:
        return ConsumedDeepLink.objects.filter(jti=jti).exists()

    def consume(self, jti: str, ref: str, exp: datetime) -> bool:
        try:
            with transaction.atomic():
                ConsumedDeepLink.objects.create(jti=jti, ref=ref, exp=exp)
        except IntegrityError:
            return False
        return True

    def sweep(self, now: datetime) -> int:
        return ConsumedDeepLink.objects.filter(exp__lt=now).delete()[0]


# --- phase 3 ----------------------------------------------------------------------------------


class OrmAuthAttemptStore(AuthAttemptStore):
    def create(self, a: m.AuthAttempt) -> str:
        AuthAttemptRecord.objects.create(
            state=a.state,
            code_verifier_enc=encrypt_str(a.code_verifier),
            participant=participant_to_json(a.participant),
            participant_secrets_enc=encrypt_secrets(a.participant.secrets),
            location_id=a.location_id,
            vendor=a.vendor,
            iss=a.iss,
            scopes=a.scopes,
            redirect_uri=a.redirect_uri,
            token_endpoint=a.token_endpoint,
            destination=a.destination,
            created_at=a.created_at,
            expires_at=a.expires_at,
        )
        return a.state

    def consume(self, state: str) -> m.AuthAttempt:
        now = timezone.now()
        with transaction.atomic():
            updated = AuthAttemptRecord.objects.filter(state=state, used_at__isnull=True, expires_at__gt=now).update(
                used_at=now
            )
            if not updated:
                raise KeyError(state)
            r = AuthAttemptRecord.objects.get(state=state)
        return m.AuthAttempt(
            state=r.state,
            code_verifier=decrypt_str(r.code_verifier_enc),
            participant=participant_from_json(r.participant, r.participant_secrets_enc),
            location_id=r.location_id,
            vendor=r.vendor,
            iss=r.iss,
            scopes=list(r.scopes),
            redirect_uri=r.redirect_uri,
            token_endpoint=r.token_endpoint,
            destination=r.destination,
            created_at=r.created_at,
            expires_at=r.expires_at,
            used_at=r.used_at,
        )

    def sweep(self, older_than: timedelta) -> int:
        return AuthAttemptRecord.objects.filter(expires_at__lt=timezone.now() - older_than).delete()[0]


# --- jobs -------------------------------------------------------------------------------------

_SAVED_FIELDS = [
    "status",
    "participant",
    "participant_secrets_enc",
    "source",
    "destination",
    "credentials_enc",
    "token_expires_at",
    "outcomes",
    "documents",
    "spool_ref",
    "receipt",
    "failure",
    "attempts",
    "finished_at",
    "updated_at",
]


def _apply(job: m.Job, r: JobRecord) -> JobRecord:
    r.status = str(job.status)
    r.participant = participant_to_json(job.participant)
    r.participant_secrets_enc = encrypt_secrets(job.participant.secrets)
    r.source = asdict(job.source)
    r.destination = job.destination
    r.credentials_enc = encrypt_secrets(job.credentials)
    r.token_expires_at = job.token_expires_at
    r.outcomes = [asdict(o) for o in job.outcomes]
    r.documents = asdict(job.documents)
    r.spool_ref = job.spool_ref
    r.receipt = asdict(job.receipt) if job.receipt else None
    r.failure = job.failure
    r.attempts = job.attempts
    r.finished_at = job.finished_at
    return r


def job_from_record(r: JobRecord) -> m.Job:
    return m.Job(
        id=r.id,
        participant=participant_from_json(r.participant, r.participant_secrets_enc),
        source=m.Source(**r.source),
        destination=r.destination,
        status=m.JobStatus(r.status),
        credentials=decrypt_secrets(r.credentials_enc),
        token_expires_at=r.token_expires_at,
        outcomes=[m.TypeOutcome(**o) for o in r.outcomes],
        documents=m.DocumentOutcome(**(r.documents or {})),
        spool_ref=r.spool_ref,
        receipt=_receipt_from_json(r.receipt),
        failure=r.failure,
        attempts=r.attempts,
        created_at=r.created_at,
        updated_at=r.updated_at,
        finished_at=r.finished_at,
    )


class OrmJobStore(JobStore):
    def create(self, job: m.Job) -> m.JobId:
        r = _apply(job, JobRecord(id=job.id) if job.id else JobRecord())
        r.save(force_insert=True)
        job.id = r.id
        job.created_at = r.created_at
        return r.id

    def get(self, job_id: m.JobId) -> m.Job:
        try:
            return job_from_record(JobRecord.objects.get(id=job_id))
        except JobRecord.DoesNotExist:
            raise KeyError(job_id) from None

    def save(self, job: m.Job) -> None:
        r = JobRecord.objects.get(id=job.id)
        _apply(job, r).save(update_fields=_SAVED_FIELDS)


class OrmJobClaimer(JobClaimer):
    """DbPollingDispatcher's claimer: SELECT ... FOR UPDATE SKIP LOCKED over the job rows."""

    RETRY_AFTER = timedelta(seconds=30)

    def __init__(self, max_attempts: int = 3):
        self.max_attempts = max_attempts

    def claim_next(self, worker_id: str, lease: timedelta) -> m.JobId | None:
        now = timezone.now()
        # A non-terminal job with no lease is runnable: queued ones have never run, and fetching /
        # delivering ones with no lease were abandoned by a process that released at exit.
        runnable = Q(status__in=["queued", "fetching", "fetched", "delivering"]) | Q(
            status="failed", attempts__lt=self.max_attempts, updated_at__lt=now - self.RETRY_AFTER
        )
        # A stale lease means a worker died mid-run; its job is resumable by phase.
        stale = Q(lease_expires_at__lt=now) & ~Q(status__in=["done", "failed_terminal"])
        with transaction.atomic():
            r = (
                JobRecord.objects.select_for_update(skip_locked=True)
                .filter((runnable & Q(lease_expires_at__isnull=True)) | stale)
                .order_by("created_at")
                .first()
            )
            if r is None:
                return None
            JobRecord.objects.filter(id=r.id).update(lease_owner=worker_id, lease_expires_at=now + lease)
            return r.id

    def claim(self, job_id: m.JobId, worker_id: str, lease: timedelta) -> bool:
        """Lease one named job (broker-driven workers): only if it is not terminal and not leased."""
        now = timezone.now()
        return bool(
            JobRecord.objects.filter(id=job_id)
            .exclude(status__in=["done", "failed_terminal"])
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lt=now))
            .update(lease_owner=worker_id, lease_expires_at=now + lease)
        )

    def release(self, job_id: m.JobId, worker_id: str) -> None:
        JobRecord.objects.filter(id=job_id, lease_owner=worker_id).update(lease_owner=None, lease_expires_at=None)

    def live_spool_refs(self) -> set[str]:
        refs = (
            JobRecord.objects.exclude(status__in=["done", "failed_terminal"])
            .exclude(spool_ref__isnull=True)
            .values_list("spool_ref", flat=True)
        )
        return {r for r in refs if r}
