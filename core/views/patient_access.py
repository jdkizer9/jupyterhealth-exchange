"""USCDI Patient Access: a second patient-facing client beside the EHR Patient Portal (sketch).

The EHR Patient Portal pulls records in the browser into JHE's FHIR store; this client runs the
EHR sign-in, the pull and the delivery server-side through the Patient Access Module, in a
thread of this process (cron sweeps up any a restart drops), and delivers to every configured
destination (JHE's import and the study's bucket). Nothing here touches the EHR Patient Portal
client, its routes, its JavaScript or its templates: the picker and checklist are copies.

    GET  /clients/uscdi-patient-access/                 the patient-facing shell (invitation code -> hub -> ...)
    POST /api/v1/uscdi-patient-access/launch            {location_id, scopes, data_source_id} -> {authorize_url}
    GET  /clients/uscdi-patient-access/callback         the EHR's return URL: finish sign-in, create the job
    GET  /api/v1/uscdi-patient-access/jobs/<id>         the patient's own job: status, per-type rows, receipts
    GET  /.well-known/jwks.json                         the module's client signing keys (public halves)
"""

from urllib.parse import urlencode

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from patient_access.core.flow import FlowError, finish_upstream_auth, start_upstream_auth
from patient_access.django_apps.jobs.dispatch import OnCommitDispatcher
from patient_access.django_apps.jobs.wiring import get_module
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import DataSource, EhrBrandLocation, EhrVendor, Patient
from core.patient_access.identity import PROVIDER, participant_for
from core.services.fhir_source import create_pull_fhir_source
from core.views.patient_facing import patient_facing_config

CLIENT_NAME = "USCDI Patient Access"
CLIENT_KEY = "uscdi-patient-access"


def _config():
    config = patient_facing_config(CLIENT_NAME, CLIENT_KEY, reverse("uscdi-patient-access-connect"))
    # Every type the module can pull, for the consent and receipt screens (the actual request is
    # vendor-specific and chosen at the checklist).
    config["expectedResourceTypes"] = sorted(
        {token.removeprefix("patient/").removesuffix(".read") for token in EhrVendor.SUPPORTED_SCOPES}
    )
    return config


def connect(request):
    """Patient-facing start page: invitation -> hub -> consent -> hospital picker -> checklist -> server-side launch."""
    return render(request, "clients/uscdi-patient-access/connect.html", {"config": _config()})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def launch(request):
    patient = request.user.get_patient()
    if patient is None:
        return Response({"error": "Authenticated user is not a patient"}, status=400)
    data_source_id = request.data.get("data_source_id")
    if not DataSource.objects.filter(id=data_source_id).exists():
        return Response({"error": "Unknown data source"}, status=400)
    # The consent screen comes first in the shell; the sketch does not re-check it here.
    m = get_module()
    location_id = str(request.data.get("location_id", ""))
    scopes = list(request.data.get("scopes") or [])
    # What the checklist offered and the patient unchecked, for the receipt.
    location = (
        EhrBrandLocation.objects.select_related("brand__vendor").filter(id=int(location_id)).first()
        if location_id.isdigit()
        else None
    )
    offered = (location.brand.vendor.supported_scopes or "").split() if location else []
    try:
        url = start_upstream_auth(
            participant=participant_for(patient, data_source_id),
            selection_id=location_id,
            directory=m.directory,
            keyring=m.keyring,
            scopes_declined=[s for s in offered if s not in scopes],
            attempts=m.auth_attempts,
            redirect_uri=m.redirect_uri,
            destinations=m.default_destinations,
            http=m.http,
            clock=m.clock,
            scopes=scopes,
        )
    except FlowError as e:
        return Response({"error": str(e)}, status=400)
    return Response({"authorize_url": url})


def callback(request):
    """The EHR's return URL: the module exchanges the code server-side, creates the job and
    starts it, then the shell's status route follows it. Errors go to the shell's error route."""
    page = reverse("uscdi-patient-access-connect")
    m = get_module()
    try:
        # One transaction: the attempt is spent, the job and the pull's FhirSource created
        # together; the thread starts on commit.
        with transaction.atomic():
            job_id = finish_upstream_auth(
                state=request.GET.get("state", ""),
                code=request.GET.get("code"),
                error=request.GET.get("error"),
                error_description=request.GET.get("error_description"),
                directory=m.directory,
                keyring=m.keyring,
                attempts=m.auth_attempts,
                identity=m.identity,
                jobs=m.jobs,
                dispatcher=OnCommitDispatcher(m.dispatcher),
                http=m.http,
                clock=m.clock,
            )
            # The pull's own FhirSource, created now so its id is on the job before any sink
            # runs: the S3 key is {patient_id}/{fhir_source_id} and JHE's import lands under it.
            job = m.jobs.get(job_id)
            patient = Patient.objects.get(id=int(job.participant.ref))
            location = (
                EhrBrandLocation.objects.filter(id=int(job.source.facility_id)).first()
                if job.source.facility_id
                else None
            )
            fhir_source = create_pull_fhir_source(
                patient,
                DataSource.objects.get(id=job.participant.attrs["data_source_id"]),
                ehr_brand_location=location,
                base_url=job.source.iss,
                when=job.created_at,
            )
            job.participant.attrs["patient_id"] = patient.id
            job.participant.attrs["fhir_source_id"] = fhir_source.id
            m.jobs.save(job)
    except FlowError as e:
        query = urlencode(
            {"route": "error", "title": "We couldn't connect to your healthcare organization", "message": str(e)}
        )
        return redirect(f"{page}?{query}")
    data_source_id = job.participant.attrs.get("data_source_id")
    return redirect(f"{page}?{urlencode({'route': 'status', 'job': job_id, 'source': data_source_id})}")


def _receipt_json(receipt):
    return {
        "destination": receipt.destination,
        "location": receipt.location,
        "resources": receipt.resources,
        "rejected": receipt.details.get("rejected", 0),
        "blobs": receipt.blobs,
        "bytes": receipt.bytes,
        "failures": receipt.failures,
        "warnings": receipt.warnings,
        "fhir_source_id": receipt.details.get("fhir_source_id"),
    }


def job_json(job):
    return {
        "id": job.id,
        "status": str(job.status),
        "terminal": job.status.terminal,
        "data_source_id": job.participant.attrs.get("data_source_id"),
        "fhir_source_id": job.participant.attrs.get("fhir_source_id"),
        "source": {"brand": job.source.brand_name, "location": job.source.facility_name},
        "types": [{"label": o.label, "status": o.status, "count": o.count, "error": o.error} for o in job.outcomes],
        # One entry per destination (design §14): JHE's import and the study's bucket each report.
        "deliveries": {
            name: {
                "status": d.status,
                "attempts": d.attempts,
                "failure": d.failure,
                "receipt": _receipt_json(d.receipt) if d.receipt else None,
            }
            for name, d in job.deliveries.items()
        },
        "failure": job.failure,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def job_status(request, job_id):
    patient = request.user.get_patient()
    try:
        job = get_module().jobs.get(job_id)
    except KeyError:
        job = None
    if job is None or patient is None or job.participant.provider != PROVIDER or job.participant.ref != str(patient.id):
        return Response({"error": "Not found"}, status=404)
    return Response(job_json(job))


def jwks(request):
    """The Patient Access Module's client signing keys (public halves), registered with EHR vendors
    as our JWK Set URL for private_key_jwt. Separate from the OIDC provider's keys."""
    response = JsonResponse(get_module().keyring.keys.public_jwks())
    response["Cache-Control"] = "public, max-age=300"
    return response
