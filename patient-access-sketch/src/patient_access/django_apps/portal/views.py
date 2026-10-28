"""The one web layer (phases 1-3 and status). Thin: everything it decides comes from core or a plug."""

from __future__ import annotations

from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from patient_access.core.flow import FlowError, finish_upstream_auth, start_upstream_auth
from patient_access.core.interfaces import IdentityError
from patient_access.core.models import InboundRequest, Participant
from patient_access.django_apps.jobs.crypto import encrypt_secrets
from patient_access.django_apps.jobs.stores import participant_from_json, participant_to_json
from patient_access.django_apps.jobs.wiring import get_module

SESSION_KEY = "pa_participant"


def health(request: HttpRequest) -> JsonResponse:
    with connection.cursor() as cur:
        cur.execute("select 1")
    return JsonResponse({"ok": True})


# --- session binding --------------------------------------------------------------------------


def _bind(request: HttpRequest, participant: Participant) -> None:
    request.session.cycle_key()  # no fixation across the identity boundary
    request.session[SESSION_KEY] = {
        **participant_to_json(participant),
        "secrets_enc": encrypt_secrets(participant.secrets),
    }


def _participant(request: HttpRequest) -> Participant | None:
    data = request.session.get(SESSION_KEY)
    if not data:
        return None
    return participant_from_json(data, data.get("secrets_enc", ""))


def _error(request: HttpRequest, message: str, status: int = 400) -> HttpResponse:
    return render(request, "patient_access/error.html", {"message": message}, status=status)


def _inbound(request: HttpRequest) -> InboundRequest:
    return InboundRequest(
        query={k: str(v) for k, v in request.GET.items()},
        headers={k.lower(): str(v) for k, v in request.headers.items()},
    )


# --- phase 1 ----------------------------------------------------------------------------------


@require_GET
def start(request: HttpRequest) -> HttpResponse:
    module = get_module()
    try:
        participant = module.identity.resolve(_inbound(request))
    except IdentityError as e:
        return _error(request, str(e))
    _bind(request, participant)
    brand = participant.attrs.get("brand")
    if brand:
        try:
            location = module.directory.location(brand)
        except KeyError:
            return redirect("patient_access:pick")  # stale brand in the link: fall back to the picker
        return render(request, "patient_access/confirm.html", {"location": location})
    return redirect("patient_access:pick")


# --- phase 2 ----------------------------------------------------------------------------------


@require_GET
def pick(request: HttpRequest) -> HttpResponse:
    if _participant(request) is None:
        return _error(request, "Please open the personal link from your invitation first.", 403)
    q = request.GET.get("q", "").strip()
    state = request.GET.get("state", "").strip() or None
    results = get_module().directory.search(q, state=state) if (q or state) else []
    return render(request, "patient_access/pick.html", {"q": q, "state": state or "", "results": results})


@require_GET
def api_brands(request: HttpRequest) -> JsonResponse:
    if _participant(request) is None:
        return JsonResponse({"error": "no participant"}, status=403)
    results = get_module().directory.search(request.GET.get("q", ""), state=request.GET.get("state") or None)
    # No client ids or vendor config leave the server: the server launches.
    return JsonResponse(
        {"results": [{"id": r.id, "name": r.name, "brand": r.brand_name, "address": r.address} for r in results]}
    )


@require_POST
def launch(request: HttpRequest) -> HttpResponse:
    participant = _participant(request)
    if participant is None:
        return _error(request, "Please open the personal link from your invitation first.", 403)
    m = get_module()
    try:
        url = start_upstream_auth(
            participant=participant,
            location_id=request.POST.get("location_id", ""),
            directory=m.directory,
            vendors=m.vendors,
            attempts=m.auth_attempts,
            redirect_uri=m.redirect_uri,
            destination=m.default_destination,
            http=m.http,
            clock=m.clock,
        )
    except FlowError as e:
        return _error(request, str(e))
    return redirect(url)


# --- phase 3 callback -------------------------------------------------------------------------


@require_GET
def callback(request: HttpRequest) -> HttpResponse:
    m = get_module()
    try:
        # One transaction: spending the identity and creating the job must not half-happen.
        with transaction.atomic():
            job_id = finish_upstream_auth(
                state=request.GET.get("state", ""),
                code=request.GET.get("code"),
                error=request.GET.get("error"),
                error_description=request.GET.get("error_description"),
                directory=m.directory,
                vendors=m.vendors,
                attempts=m.auth_attempts,
                identity=m.identity,
                jobs=m.jobs,
                dispatcher=m.dispatcher,
                http=m.http,
                clock=m.clock,
            )
    except FlowError as e:
        return _error(request, str(e))
    # The attempt carried the participant, so the callback works even if the session cookie did
    # not survive the EHR round trip; record the job on whatever session we have.
    request.session.setdefault("pa_jobs", [])
    request.session["pa_jobs"] = [*request.session["pa_jobs"], job_id]
    return redirect("patient_access:status", job_id=job_id)


# --- status -----------------------------------------------------------------------------------


def _visible_job(request: HttpRequest, job_id: str):
    """A job is visible to the session that created it, or to its participant."""
    participant = _participant(request)
    try:
        job = get_module().jobs.get(job_id)
    except KeyError:
        return None
    if job_id in request.session.get("pa_jobs", []) or (participant and participant.ref == job.participant.ref):
        return job
    return None


def _job_json(job) -> dict:
    return {
        "id": job.id,
        "status": str(job.status),
        "terminal": job.status.terminal,
        "source": {"brand": job.source.brand_name, "location": job.source.location_name},
        "outcomes": [
            {
                "label": o.label,
                "status": o.status,
                "count": o.count,
                "pages": o.pages,
                "error": o.error,
                "capped": o.capped,
            }
            for o in job.outcomes
        ],
        "documents": vars(job.documents),
        "receipt": vars(job.receipt) if job.receipt else None,
        "failure": job.failure,
        "attempts": job.attempts,
    }


@require_GET
def status(request: HttpRequest, job_id: str) -> HttpResponse:
    job = _visible_job(request, job_id)
    if job is None:
        return _error(request, "We couldn't find that request.", 404)
    return render(request, "patient_access/status.html", {"job": _job_json(job)})


@require_GET
def api_job(request: HttpRequest, job_id: str) -> JsonResponse:
    job = _visible_job(request, job_id)
    if job is None:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse(_job_json(job))
