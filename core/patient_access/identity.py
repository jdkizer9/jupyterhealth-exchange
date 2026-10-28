"""The participant is the JHE patient behind the bearer token; JHE's own auth did phase 1."""

from patient_access.core.interfaces import IdentityError, IdentityProvider
from patient_access.core.models import Participant

PROVIDER = "jhe_user"


def participant_for(patient, data_source_id):
    """`ref` is the Patient pk; the sink reads `data_source_id` to register the FhirSource."""
    return Participant(ref=str(patient.id), provider=PROVIDER, attrs={"data_source_id": int(data_source_id)})


class JheUserIdentity(IdentityProvider):
    name = PROVIDER

    def resolve(self, request):
        raise IdentityError("JHE identifies the patient by its API token, not by a link.")

    def commit(self, participant):
        """Nothing to spend: the invitation was already redeemed for the token."""
