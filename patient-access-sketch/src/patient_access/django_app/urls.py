from django.urls import path

from . import views

app_name = "patient_access"

urlpatterns = [
    path("health", views.health, name="health"),
    path("start", views.start, name="start"),
    path("pick", views.pick, name="pick"),
    path("api/brands", views.api_brands, name="api_brands"),
    path("launch", views.launch, name="launch"),
]
