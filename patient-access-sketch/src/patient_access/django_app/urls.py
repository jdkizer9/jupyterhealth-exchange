from django.urls import path

from . import views

app_name = "patient_access"

urlpatterns = [
    path("health", views.health, name="health"),
]
