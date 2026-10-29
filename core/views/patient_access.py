"""Patient Access Module endpoints for the EHR Patient Portal (sketch, scenario D).

The portal's JS keeps its screens; these replace the browser-side SMART flow. The module runs
the EHR sign-in and the pull server-side, in a thread of this process (cron sweeps up any a
restart drops), so closing the tab no longer stops a pull.

    POST /api/v1/patient-access/launch       {location_id, scopes, data_source_id} -> {authorize_url}
    GET  /api/v1/patient-access/jobs/<id>    the patient's own job: status, per-type rows, receipt
    GET  /clients/ehr-patient-portal/callback (ehr_patient_portal.py) finishes sign-in, starts the job
"""

from django.http import JsonResponse
from patient_access.core.flow import FlowError, start_upstream_auth
from patient_access.django_apps.jobs.wiring import get_module
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import DataSource, EhrBrandLocation
from core.patient_access.identity import PROVIDER, participant_for


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def launch(request):
    patient = request.user.get_patient()
    if patient is None:
        return Response({"error": "Authenticated user is not a patient"}, status=400)
    data_source_id = request.data.get("data_source_id")
    if not DataSource.objects.filter(id=data_source_id).exists():
        return Response({"error": "Unknown data source"}, status=400)
    # The consent screen comes first in the portal; the sketch does not re-check it here.
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
        "source": {"brand": job.source.brand_name, "location": job.source.facility_name},
        "types": [{"label": o.label, "status": o.status, "count": o.count, "error": o.error} for o in job.outcomes],
        # One entry per destination (design §14): JHE's import and the study's S3 bucket each report.
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
