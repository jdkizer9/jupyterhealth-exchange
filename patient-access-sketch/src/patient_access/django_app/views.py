"""The one web layer (phases 1-3 and status). Thin: everything it decides comes from core or a plug."""

from __future__ import annotations

from django.db import connection
from django.http import HttpRequest, JsonResponse


def health(request: HttpRequest) -> JsonResponse:
    with connection.cursor() as cur:
        cur.execute("select 1")
    return JsonResponse({"ok": True})
