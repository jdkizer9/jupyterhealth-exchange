"""Minimal standalone host for the sketch (the standalone-consumer shape).

Every PA_* value is required: dev values live in docker-compose.yml, never as fallbacks here.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError:
        raise RuntimeError(f"{name} must be set (see docker-compose.yml)") from None


DEBUG = os.environ.get("PA_DEBUG") == "1"
SECRET_KEY = env("PA_SECRET_KEY")
ALLOWED_HOSTS = env("PA_ALLOWED_HOSTS").split(",")
SITE_URL = env("PA_SITE_URL").rstrip("/")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    # The standalone server uses every plug, so it installs every app; a host like JHE installs
    # only `jobs` (see patient_access/django_apps/__init__.py).
    "patient_access.django_apps.jobs",
    "patient_access.django_apps.directory",
    "patient_access.django_apps.deeplink",
    "patient_access.django_apps.portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "server.urls"
WSGI_APPLICATION = "server.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env("PA_DB_HOST"),
        "PORT": env("PA_DB_PORT"),
        "NAME": env("PA_DB_NAME"),
        "USER": env("PA_DB_USER"),
        "PASSWORD": env("PA_DB_PASSWORD"),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"

# The EHR redirects back here with a top-level GET, so Lax is enough for the session cookie.
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# The module's deployment config: which plugs, their options, vendors and query tables.
PATIENT_ACCESS_CONFIG = env("PA_CONFIG")
# Fernet key for credentials at rest.
PATIENT_ACCESS_DEPLOYMENT_KEY = env("PA_DEPLOYMENT_KEY")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"urllib3": {"level": "WARNING"}},
}
