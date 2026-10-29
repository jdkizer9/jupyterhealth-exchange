"""Dev: put the SMART sandbox "brands" from PATIENT_ACCESS["SEED"] into JHE's EHR tables.

Each brand's iss encodes the sandbox launch options (client type, PKCE, scripted login), so
the rows are rebuilt on every run. The vendors get every supported scope so the "choose what to
share" step offers them all.
"""

from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from patient_access.django_apps.jobs.conf import patient_access_settings
from patient_access.testing.smart_sandbox import SandboxConfig, jwks_from_public_pem

from core.models import EhrBrand, EhrBrandLocation, EhrVendor


class Command(BaseCommand):
    help = "Seed the SMART sandbox brands named in PATIENT_ACCESS['SEED']['smart_sandbox']."

    @transaction.atomic
    def handle(self, *args, **options):
        entries = patient_access_settings.SEED.get("smart_sandbox", [])
        vendors = patient_access_settings.VENDORS
        for name in {e["vendor"] for e in entries}:
            vendor, _ = EhrVendor.objects.get_or_create(name=name)
            vendor.ehr_client_id = vendors[name]["client_id"]
            vendor.supported_scopes = " ".join(EhrVendor.SUPPORTED_SCOPES)
            vendor.save()
            EhrBrand.objects.filter(vendor=vendor).delete()
        for e in entries:
            launch = dict(e["launch"])
            if pub := launch.pop("jwks_public_key", None):
                launch["jwks"] = jwks_from_public_pem(Path(pub).read_text(), launch.pop("kid"), launch.pop("alg"))
            iss = SandboxConfig(client_id=vendors[e["vendor"]]["client_id"], **launch).iss()
            brand = EhrBrand.objects.create(
                name=e["name"], vendor=EhrVendor.objects.get(name=e["vendor"]), fhir_base_url=iss
            )
            EhrBrandLocation.objects.create(
                brand=brand,
                name=e["name"],
                address_text=f"{e['line']}, {e['city']}, {e['state']} {e['postal']}",
                city=e["city"],
                state=e["state"],
                postal_code=e["postal"],
                country="US",
            )
            self.stdout.write(f"{e['name']} ({e['vendor']})")
