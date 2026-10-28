from django.urls import include, path

urlpatterns = [
    path("", include("patient_access.django_apps.portal.urls")),
]
