"""The one web layer (phases 1-3 and status). Thin: everything it decides comes from core or a plug."""

from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from patient_access.core.interfaces import IdentityError
from patient_access.core.models import InboundRequest, Participant

from .crypto import encrypt_secrets
from .stores import participant_from_json, participant_to_json
from .wiring import get_module

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
        query={k: v for k, v in request.GET.items()},
        headers={k.lower(): v for k, v in request.headers.items()},
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
    try:
        location = get_module().directory.location(request.POST.get("location_id", ""))
    except KeyError:
        return _error(request, "That organization is not in the directory.")
    return render(request, "patient_access/launch_stub.html", {"location": location, "participant": participant})
