from django.urls import path

from . import views

app_name = "patient_access"

urlpatterns = [
    path("health", views.health, name="health"),
    path(".well-known/jwks.json", views.jwks, name="jwks"),
    path("start", views.start, name="start"),
    path("pick", views.pick, name="pick"),
    path("api/brands", views.api_brands, name="api_brands"),
    path("launch", views.launch, name="launch"),
    path("callback", views.callback, name="callback"),
    path("status/<str:job_id>", views.status, name="status"),
    path("api/jobs/<str:job_id>", views.api_job, name="api_job"),
]
