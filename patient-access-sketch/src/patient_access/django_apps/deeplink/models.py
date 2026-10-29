"""JwtDeepLinkIdentity's storage."""

from django.db import models


class ConsumedDeepLink(models.Model):
    """JwtDeepLinkIdentity's DeepLinkStore: a jti is inserted when its link creates a job."""

    jti = models.CharField(primary_key=True)
    ref = models.CharField()
    exp = models.DateTimeField()
    used_at = models.DateTimeField(auto_now_add=True)
