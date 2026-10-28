from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from core.fhir.ref_indexing import index_fhir_source_refs
from core.models import FhirSource
from core.serializers import FhirSourceSerializer
from core.services.fhir_source import get_or_create_ehr_source


class FhirSourceViewSet(ModelViewSet):
    """Simple CRUD for a patient to register and manage their own FhirSources.

    Scoped to the requesting patient user: a patient only ever sees and edits their own
    sources, and ``patient`` is assigned from the authenticated user on create.
    """

    serializer_class = FhirSourceSerializer
    model_class = FhirSource

    def get_queryset(self):
        patient = self.request.user.get_patient()
        if patient is None:
            return FhirSource.objects.none()
        return FhirSource.objects.filter(patient=patient).select_related("ehr_brand_location").order_by("-last_updated")

    def create(self, request, *args, **kwargs):
        """Register a source, or return the patient's existing one for the same EHR brand (200 existing / 201 new).

        The rule lives in ``core.services.fhir_source.get_or_create_ehr_source`` so in-process callers share it.
        """
        patient = request.user.get_patient()
        if patient is None:
            raise PermissionDenied("Only patient users can register a FhirSource.")
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        source, created = get_or_create_ehr_source(
            patient,
            data["data_source"],
            ehr_brand_location=data.get("ehr_brand_location"),
            # Not a model field: a lookup hint for the brand when no facility was picked.
            base_url=data.get("ehr_base_url", ""),
            label=data.get("label", ""),
        )
        return Response(
            self.get_serializer(source).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK
        )

    @action(detail=True, methods=["POST"])
    def index_refs(self, request, pk=None):
        """Rewrite this source's aux-resource references from upstream ids to JHE ids (#584).

        get_object() enforces ownership (the queryset is the caller's own sources). Returns a
        summary of rows indexed and references rewritten / not found.
        """
        fhir_source = self.get_object()
        return Response(index_fhir_source_refs(fhir_source))
