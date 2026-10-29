"""R4 import for in-process callers (the Patient Access Module's sink), without an HTTP hop.

Sketch shape: the create path's handlers read the acting user and the ``X-JHE-FHIR-Source-ID``
header off the request, so this runs the import view's own per-entry processing under a
request-shaped context carrying exactly those. Same conversion, validation, routing, provenance
and batch-response as ``POST /fhir-import/R4/``. The real refactor gives the handlers a write
context (user, source) instead of a request, and the view and this function both build one.
"""

from core.views.fhir import FHIR_SOURCE_ID_HEADER
from core.views.fhir_import import FHIRImportView


class _WriteContext:
    """The parts of a request the FHIR create path reads."""

    method = "POST"

    def __init__(self, user, fhir_source):
        self.user = user
        self.headers = {FHIR_SOURCE_ID_HEADER: str(fhir_source.pk)}
        self.GET = {}


def import_r4_bundle(user, fhir_source, bundle):
    """Import an R4 Bundle under ``fhir_source`` as ``user``; returns the batch-response Bundle.

    ``user`` must be allowed to write to the source (its patient, or a practitioner who manages
    that patient's data), exactly as for the HTTP endpoint; a refusal comes back per entry.
    """
    view = FHIRImportView()
    view.request = _WriteContext(user, fhir_source)
    return {"resourceType": "Bundle", "type": "batch-response", "entry": view._process_bundle(bundle)}
