"""Load the dev directory: Brands Bundles from files, plus SMART sandbox "brands" built from launch options.

Idempotent; sandbox brands are rebuilt each run because their iss encodes the launch options.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand

from patient_access.django_apps.jobs.conf import patient_access_settings
from patient_access.django_apps.jobs.wiring import get_module
from patient_access.testing.smart_sandbox import SandboxConfig, jwks_from_public_pem

from ...brands import brands_bundle
from ...models import EhrBrand


class Command(BaseCommand):
    help = 'Seed the directory from PATIENT_ACCESS["SEED"].'

    def handle(self, *args, **options):
        seed = patient_access_settings.SEED
        module = get_module()
        for b in seed.get("bundles", []):
            counts = module.directory.upsert_brands_bundle(json.loads(Path(b["file"]).read_text()), b["vendor"])
            self.stdout.write(f"{b['file']}: {counts.brands} brands, {counts.locations} locations ({b['vendor']})")
        sandbox = seed.get("smart_sandbox", [])
        if sandbox:
            EhrBrand.objects.filter(endpoint__vendor__name__in={s["vendor"] for s in sandbox}).delete()
        for s in sandbox:
            registration = module.vendors[s["vendor"]].registration
            launch = dict(s["launch"])
            if pub := launch.pop("jwks_public_key", None):
                launch["jwks"] = jwks_from_public_pem(
                    Path(pub).read_text(), launch.pop("kid"), launch.pop("alg", "RS384")
                )
            iss = SandboxConfig(client_id=registration.client_id, **launch).iss()
            counts = module.directory.upsert_brands_bundle(brands_bundle([{**s, "iss": iss}]), s["vendor"])
            self.stdout.write(f"sandbox {s['id']}: {counts.locations} location ({s['vendor']})")
