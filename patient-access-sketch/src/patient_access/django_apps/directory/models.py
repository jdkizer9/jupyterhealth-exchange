"""The module's own directory, shaped like the SMART User-access Brands format (design §10.8).

Only for hosts without a directory of their own. Facilities under brands, brands on endpoints,
endpoints under a customer (the installation) and a vendor (the registration key).
"""

from django.db import models


class EhrVendor(models.Model):
    """A key into the deployment's registrations ("Epic Sandbox", "Epic Production")."""

    name = models.CharField(unique=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class EhrCustomer(models.Model):
    """The installation behind one or more endpoints (Epic's `Endpoint.managingOrganization`):
    one authorization server, per-customer credentials, site-specific behaviour."""

    vendor = models.ForeignKey(EhrVendor, on_delete=models.PROTECT, related_name="customers")
    external_id = models.CharField()  # the vendor's id for it, e.g. Epic organization id
    name = models.CharField(blank=True, default="")
    overrides = models.JSONField(default=dict, blank=True)  # apply_overrides format
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["vendor", "external_id"], name="pa_customer_per_vendor")]

    def __str__(self):
        return self.name or self.external_id


class EhrEndpoint(models.Model):
    """A FHIR server: the SMART `iss`. Several brands may share one."""

    vendor = models.ForeignKey(EhrVendor, on_delete=models.PROTECT, related_name="endpoints")
    customer = models.ForeignKey(
        EhrCustomer, null=True, blank=True, on_delete=models.SET_NULL, related_name="endpoints"
    )
    address = models.TextField(unique=True)
    fhir_version = models.CharField(null=True, blank=True)
    overrides = models.JSONField(default=dict, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.address


class EhrBrand(models.Model):
    """The patient-recognisable organisation."""

    name = models.CharField()
    endpoint = models.ForeignKey(EhrEndpoint, on_delete=models.CASCADE, related_name="brands")
    source_id = models.CharField(null=True, blank=True)  # the bundle's Organization id, for re-import
    npi_type_2 = models.CharField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["name"])]

    def __str__(self):
        return self.name


class EhrFacility(models.Model):
    """A care location under a brand: what the patient searches by."""

    brand = models.ForeignKey(EhrBrand, on_delete=models.CASCADE, related_name="facilities")
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
