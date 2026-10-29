# Runs inside the JHE container (`manage.py shell`), not the sketch. Idempotent.
# Adds one JHE client, "Patient Access Sketch", whose invitation link lands on the sketch, linked
# to the existing "EHR Patient Portal" DataSource and to the seeded BP & HR study so it is
# invitable; then issues an invitation for the seeded patient Peter and prints the link.
# Undo: delete the Application named "Patient Access Sketch" (or scripts/dev/reset.sh in JHE).
from core.models import ClientDataSource, DataSource, JheClient, Patient, PatientInvitation, StudyClient
from django.conf import settings
from oauth2_provider.models import get_application_model

Application = get_application_model()
NAME = "Patient Access Sketch"
app, created = Application.objects.get_or_create(
    name=NAME,
    defaults={
        "client_id": "patient-access-sketch-dev",
        "client_type": Application.CLIENT_PUBLIC,
        "authorization_grant_type": Application.GRANT_AUTHORIZATION_CODE,
        "skip_authorization": True,
        "redirect_uris": settings.SITE_URL + settings.OAUTH2_CALLBACK_PATH,
        "algorithm": "RS256",
    },
)
jc, _ = JheClient.objects.get_or_create(application=app)
jc.invitation_url = "http://localhost:8080/start?code=CODE"
jc.save()
ds = DataSource.objects.get(name="EHR Patient Portal")
ClientDataSource.objects.get_or_create(client=app, data_source=ds)
portal = Application.objects.get(name="EHR Patient Portal")
for sc in StudyClient.objects.filter(client=portal):
    StudyClient.objects.get_or_create(study=sc.study, client=app)
patient = Patient.objects.get(jhe_user__email="ll_patient_peter@example.com")
invitation, link = PatientInvitation.build_link(patient, app)
print(f"DATA_SOURCE_ID={ds.id}")
print(f"PATIENT_ID={patient.id}")
print(f"INVITATION={link}")
