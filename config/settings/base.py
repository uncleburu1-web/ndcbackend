"""
NDCConnect backend — shared settings (feature-specs/02-authentication.md §10).
All secrets come from environment variables; see .env.example.
"""
from pathlib import Path

import dj_database_url
from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config("DJANGO_SECRET_KEY", default="insecure-dev-key-change-me")
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())

INSTALLED_APPS = [
    # No django.contrib.admin: platform users have no passwords, so nobody could
    # sign in to it (spec §15.6 proposal). Admin screens arrive in a later spec.
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "apps.core",
    "apps.audit",
    "apps.accounts",
    "apps.access",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True, "OPTIONS": {}}]

# --------------------------------------------------------------------------
# Database — PostgreSQL (SRS SC-001)
# --------------------------------------------------------------------------
DATABASES = {
    "default": dj_database_url.parse(
        config("DATABASE_URL", default="postgres://ndc:ndc@localhost:5432/ndcbackend"),
        conn_max_age=60,
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
# Platform users never have a usable password (spec §5.1); no password backend is needed.
AUTHENTICATION_BACKENDS: list[str] = []

# --------------------------------------------------------------------------
# Clerk (spec §6.1, §10)
# --------------------------------------------------------------------------
CLERK_SECRET_KEY = config("CLERK_SECRET_KEY", default="")
# Optional: Clerk's PEM public key for networkless verification (Dashboard → API keys → JWT public key).
CLERK_JWT_KEY = config("CLERK_JWT_KEY", default="")
CLERK_WEBHOOK_SIGNING_SECRET = config("CLERK_WEBHOOK_SIGNING_SECRET", default="")
CLERK_AUTHORIZED_PARTIES = config("CLERK_AUTHORIZED_PARTIES", default="http://localhost:5173", cast=Csv())

# --------------------------------------------------------------------------
# DRF
# --------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.accounts.authentication.ClerkSessionAuthentication"],
    # Fail closed: every endpoint needs a signed-in, active user unless it opts out.
    "DEFAULT_PERMISSION_CLASSES": ["apps.access.permissions.IsSignedIn"],
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # 0 = throttle on REMOTE_ADDR. Set to the number of proxies in front of Django in production.
    "NUM_PROXIES": config("NUM_PROXIES", default=0, cast=int),
    "DEFAULT_THROTTLE_RATES": {
        "profile_write": "20/min",
        "clerk_webhook": "300/min",
    },
}

# --------------------------------------------------------------------------
# CORS — frontend origins (spec §10)
# --------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="http://localhost:5173", cast=Csv())
CORS_ALLOW_CREDENTIALS = False  # auth is a Bearer header, not cookies

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# No CSRF middleware on purpose: the API authenticates ONLY by Bearer header and
# deliberately ignores Clerk's __session cookie (apps/accounts/clerk.py), so there
# is no ambient browser credential for a cross-site request to ride on.
SILENCED_SYSTEM_CHECKS = ["security.W003"]
