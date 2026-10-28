"""JheDirectSink: deliver a pull into JHE in-process, through the same services the HTTP API uses.

begin:  attach the EHR patient id to the JHE patient; get or create the patient's FhirSource
        for the brand (one per brand and data source)
write:  import_r4_bundle as the patient's own user; tally the batch-response
finish: receipt (no token to revoke: no JHE token was ever minted for the module)
"""

from patient_access.core.interfaces import Sink, SinkSession
from patient_access.core.models import Receipt, UnitOutcome
from patient_access.destinations.jhe import tally_batch_response

from core.models import DataSource, EhrBrandLocation, Patient, PatientIdentifier
from core.services.fhir_import import import_r4_bundle
from core.services.fhir_source import get_or_create_ehr_source


class JheDeliveryError(Exception):
    pass


class JheDirectSink(Sink):
    accepts = frozenset({"batch"})

    def begin(self, job):
        return JheDirectSession(job)


class JheDirectSession(SinkSession):
    def __init__(self, job):
        self.job = job
        try:
            self.patient = Patient.objects.select_related("jhe_user").get(id=int(job.participant.ref))
        except (Patient.DoesNotExist, ValueError):
            raise JheDeliveryError(f"no JHE patient {job.participant.ref}") from None
        source = job.source
        identifier, _ = PatientIdentifier.objects.get_or_create(
            system=source.iss, value=source.ehr_patient_id, defaults={"patient": self.patient}
        )
        if identifier.patient_id != self.patient.id:
            raise JheDeliveryError("This EHR patient is already linked to another JHE patient.")
        location = EhrBrandLocation.objects.filter(id=int(source.location_id)).first()
        self.fhir_source, self.source_created = get_or_create_ehr_source(
            self.patient,
            DataSource.objects.get(id=job.participant.attrs["data_source_id"]),
            ehr_brand_location=location,
            base_url=source.iss,
        )
        self.units = self.ok = self.failed = 0
        self.reasons = {}
        self.warnings = {}

    def write(self, unit, spool):
        out = tally_batch_response(import_r4_bundle(self.patient.jhe_user, self.fhir_source, unit.body), UnitOutcome())
        self.units += 1
        self.ok += out.ok
        self.failed += out.failed
        for k, v in out.reasons.items():
            self.reasons[k] = self.reasons.get(k, 0) + v
        for k, v in out.warnings.items():
            self.warnings[k] = self.warnings.get(k, 0) + v
        return out

    def finish(self):
        return Receipt(
            destination="jhe",
            location=f"FhirSource {self.fhir_source.id}",
            units=self.units,
            resources=self.ok,
            failures=dict(sorted(self.reasons.items(), key=lambda kv: -kv[1])),
            warnings=dict(sorted(self.warnings.items(), key=lambda kv: -kv[1])),
            details={
                "fhir_source_id": self.fhir_source.id,
                "fhir_source_created": self.source_created,
                "rejected": self.failed,
            },
        )

    def abort(self, reason):
        """Nothing held open: every unit already written stays, like the HTTP path."""
