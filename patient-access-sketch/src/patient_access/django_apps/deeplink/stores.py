"""OrmDeepLinkStore: JwtDeepLinkIdentity's DeepLinkStore over this app's table."""

from __future__ import annotations

from datetime import datetime

from django.db import IntegrityError, transaction

from patient_access.identity.jwt_deeplink import DeepLinkStore

from .models import ConsumedDeepLink


class OrmDeepLinkStore(DeepLinkStore):
    def is_consumed(self, jti: str) -> bool:
        return ConsumedDeepLink.objects.filter(jti=jti).exists()

    def consume(self, jti: str, ref: str, exp: datetime) -> bool:
        try:
            with transaction.atomic():
                ConsumedDeepLink.objects.create(jti=jti, ref=ref, exp=exp)
        except IntegrityError:
            return False
        return True

    def sweep(self, now: datetime) -> int:
        return ConsumedDeepLink.objects.filter(exp__lt=now).delete()[0]
