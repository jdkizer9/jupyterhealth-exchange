"""The module's own directory (moved from JHE's core/models/ehr_brand.py; same shape).

Only for hosts without a directory of their own."""

from django.db import models


class EhrVendor(models.Model):
    """A vendor key into the deployment's VendorConfig (client id, auth method, query table live there)."""

    name = models.CharField(unique=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class EhrBrand(models.Model):
    name = models.CharField()
    vendor = models.ForeignKey(EhrVendor, on_delete=models.PROTECT, related_name="brands")
    fhir_base_url = models.TextField(unique=True)  # the SMART iss
    npi_type_2 = models.CharField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class EhrBrandLocation(models.Model):
    brand = models.ForeignKey(EhrBrand, on_delete=models.CASCADE, related_name="locations")
    name = models.CharField()
    address_text = models.TextField(blank=True, default="")
    city = models.CharField(null=True, blank=True)
    state = models.CharField(null=True, blank=True)
    postal_code = models.CharField(null=True, blank=True)
    country = models.CharField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["state"]),
            models.Index(fields=["postal_code"]),
            models.Index(fields=["name"]),
        ]

    def __str__(self):
        return self.name
