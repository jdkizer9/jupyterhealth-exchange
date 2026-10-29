"""Dev stand-in for the consumer: print a signed deep link for a participant ref. Writes nothing."""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from patient_access.django_apps.jobs.wiring import get_module
from patient_access.identity.jwt_deeplink import JwtDeepLinkIdentity, mint_link, parse_ttl


class Command(BaseCommand):
    help = "Mint a deep link: manage.py mint_deeplink --ref P-00123 [--brand LOCATION_ID] [--ttl 7d]"

    def add_arguments(self, parser):
        parser.add_argument("--ref", required=True, help="The consumer's identifier; becomes Participant.ref.")
        parser.add_argument("--brand", help="Directory location id; skips the picker.")
        parser.add_argument("--ttl", default="7d")
        parser.add_argument("--attrs", help='JSON object echoed into the receipt, e.g. \'{"arm": "A"}\'')

    def handle(self, *args, **opts):
        identity = get_module().identity
        if not isinstance(identity, JwtDeepLinkIdentity) or not identity.mint_config:
            raise CommandError("the configured identity is not JwtDeepLinkIdentity with a `mint` section")
        mint = identity.mint_config
        self.stdout.write(
            mint_link(
                site_url=settings.SITE_URL,
                private_key=mint["private_key"],
                iss=mint["iss"],
                ref=opts["ref"],
                ttl_seconds=parse_ttl(opts["ttl"]),
                brand=opts.get("brand"),
                attrs=json.loads(opts["attrs"]) if opts.get("attrs") else None,
            )
        )
