"""
NDCConnect backend — shared settings (feature-specs/02-authentication.md §10).
All secrets come from environment variables; see .env.example.
"""
import json
from pathlib import Path

import os

import dj_database_url
from decouple import Config, Csv, RepositoryEmpty
from decouple import config as _dotenv_config

if os.environ.get("NDC_TESTS") == "1":
    # Tests (manage.py test): only real environment variables count, never .env, except where the test database is.
    _isolated = Config(RepositoryEmpty())

    def config(name, *args, **kwargs):
        return (_dotenv_config if name == "DATABASE_URL" else _isolated)(name, *args, **kwargs)
else:
    config = _dotenv_config

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
    "django.contrib.postgres",  # search: SearchVector, trigram (spec 12)
    "rest_framework",
    "corsheaders",
    "drf_spectacular",  # OpenAPI schema (spec 15 §3.2, §14)
    "apps.core",
    "apps.audit",
    "apps.accounts",
    "apps.access",
    "apps.candidates",
    "apps.follows",
    "apps.payments",
    "apps.donations",
    "apps.pvc",
    "apps.membership",
    "apps.news",
    "apps.notifications",
    "apps.posts",
    "apps.dashboard",
    "apps.search",
    "apps.moderation",
    "apps.privacy",
    "apps.integrations",  # spec 15: registry, HTTP client, webhook register
    "apps.media",  # spec 15 §7: Cloudinary / local images
    "apps.geo",  # spec 16 §8: INEC geography reference data
    "apps.communities",
    "apps.events",
    "apps.volunteers",  # spec 17
]

MIDDLEWARE = [
    "apps.core.requestid.RequestIdMiddleware",  # spec 15 §4.10: first, so every later log line carries the ID
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.SecurityHeaders",  # spec 14 §4.9
    "apps.accounts.language.LanguageMiddleware",  # spec 20 §6: request.ndc_language for spec 21
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
    "DEFAULT_SCHEMA_CLASS": "apps.core.schema.NDCAutoSchema",  # spec 15 §14
    # 0 = throttle on REMOTE_ADDR. Set to the number of proxies in front of Django in production.
    "NUM_PROXIES": config("NUM_PROXIES", default=0, cast=int),
    "DEFAULT_THROTTLE_RATES": {
        "profile_write": "20/min",
        "clerk_webhook": "300/min",
        "candidates_public": "120/min",  # generous per-IP limit against scraping bursts (spec 03 §9)
        "follows": "60/min",  # per user, follow/unfollow/notify writes (spec 04 §7.5)
        "donations_start": "10/hour",  # per IP and per user (spec 05 §7.1)
        "donations_receipt": "60/min",  # per IP — references are the key to a receipt
        "finance_public": "120/min",  # per IP
        "payment_webhook": "600/min",
        "pvc_submit": "5/hour",  # spec 06 §9
        "membership_pay": "10/hour",
        "pii_reveal": "30/hour",  # per officer — an abuse signal as much as a limit
        "news_public": "120/min",  # per IP (spec 07 §9)
        "news_submit": "5/day",  # per user
        "news_write": "30/hour",  # per user: edits, images, withdrawals, bylines
        "news_report": "5/hour",  # per user, or per IP for guests (also used for candidate-post reports, spec 09)
        "posts_write": "30/hour",  # per user: candidate post writes (spec 09 §7.2)
        "community_post_hour": "5/hour",  # spec 17 §7.4
        "community_post_day": "20/day",
        "community_comment": "30/hour",
        "community_reaction": "120/hour",
        "community_write": "60/hour",
        "event_write": "60/day",  # spec 18 §13.3 (per account)
        "event_register": "30/hour",
        "volunteer_apply": "10/day",
        "campaign_update": "20/day",
        "admin_write": "60/hour",  # per admin: grants, revokes, deactivations, reveals (spec 11 §7)
        "admin_read": "300/hour",  # per admin: overview, user search, audit search (spec 11 §9)
        "search_public": "60/min",  # per IP (spec 12 §7)
        "data_export": "3/day",  # spec 14 §4.4: per user
        "privacy_public": "10/hour",  # per user: privacy requests, consent changes, self-deletion
        "moderation_public": "5/hour",  # per user or IP: copyright notices, appeals and counter-notices (spec 13 §9)
        "media_sign": "60/hour",  # per user: upload signatures (spec 15 §7.3)
        "ready": "120/min",  # per IP: /ready/ (spec 15 §12.5)
        "geo_lookup": "120/min",  # per IP: pickers' type-ahead (spec 19 §13.1)
        "settings_write": "60/min",  # spec 20 §17
        "people_public": "60/min",
    },
}

# --------------------------------------------------------------------------
# OpenAPI (spec 15 §14). The schema is internal: system admins in production, anyone in development.
# --------------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    "TITLE": "NDCConnect API",
    "DESCRIPTION": "The NDCConnect web app's API (v1, additive changes only). Conventions: docs/api.md.",
    "VERSION": "1.0.0",
    "OAS_VERSION": "3.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_GENERATE_CHOICE_DESCRIPTION": False,
    "DISABLE_ERRORS_AND_WARNINGS": False,
    "SORT_OPERATIONS": False,
    "ENUM_NAME_OVERRIDES": {"IntegrationStateEnum": ["ok", "failing", "unavailable", "not_configured", "not_in_use"],
                            "NigerianStateEnum": "apps.candidates.reference.State",
                            "NotificationKindEnum": "apps.notifications.models.Kind",
                            "EventModeEnum": "apps.events.models.Mode",  # spec 18
                            "EventStatusEnum": "apps.events.models.EventStatus",
                            "EventRegistrationStatusEnum": "apps.events.models.RegistrationStatus",
                            "GeoDatasetKindEnum": "apps.geo.models.DatasetKind",  # spec 19
                            "GeoDatasetStatusEnum": "apps.geo.models.DatasetStatus",
                            "GeoConstituencyKindEnum": "apps.geo.models.ConstituencyKind",
                            "GeoPlaceLevelEnum": ["lga", "ward", "polling_unit"],
                            "GeoRecordKindEnum": ["membership", "pvc"],
                            "LevelEnum": "apps.communities.models.Level"},
    "APPEND_COMPONENTS": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "Clerk session JWT"}}},
    "POSTPROCESSING_HOOKS": ["drf_spectacular.hooks.postprocess_schema_enums", "apps.core.schema.postprocess"],
}

# --------------------------------------------------------------------------
# Shared cache (spec 15 §11.4). Rate limits, circuit breakers and pending uploads must be shared by every
# worker, so production uses PostgreSQL's database cache (no new technology). `memory` is per process.
# --------------------------------------------------------------------------
CACHE_BACKEND = config("CACHE_BACKEND", default="memory")  # memory | database
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "ndc_cache",
                      "OPTIONS": {"MAX_ENTRIES": 50_000, "CULL_FREQUENCY": 4}}
          if CACHE_BACKEND == "database" else {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Keyed IP hashes (spec 16 F6): a separate key so rotating DJANGO_SECRET_KEY doesn't reset abuse correlation.
IP_HASH_KEY = config("IP_HASH_KEY", default="")

# Request logging (spec 15 §12.6)
SLOW_REQUEST_MS = config("SLOW_REQUEST_MS", default=1500, cast=int)

# --------------------------------------------------------------------------
# Media (spec 15 §7). `cloudinary` in production; `local` keeps files under MEDIA_ROOT.
# CLOUDINARY_API_SECRET is backend-only. The *_BASE values exist for the fake servers used in live checks.
# --------------------------------------------------------------------------
MEDIA_BACKEND = config("MEDIA_BACKEND", default="local")
CLOUDINARY_CLOUD_NAME = config("CLOUDINARY_CLOUD_NAME", default="")
CLOUDINARY_API_KEY = config("CLOUDINARY_API_KEY", default="")
CLOUDINARY_API_SECRET = config("CLOUDINARY_API_SECRET", default="")
CLOUDINARY_UPLOAD_PRESET = config("CLOUDINARY_UPLOAD_PRESET", default="ndcconnect_signed")
CLOUDINARY_FOLDER = config("CLOUDINARY_FOLDER", default="ndcconnect/dev")
CLOUDINARY_API_BASE = config("CLOUDINARY_API_BASE", default="https://api.cloudinary.com")
CLOUDINARY_DELIVERY_BASE = config("CLOUDINARY_DELIVERY_BASE", default="https://res.cloudinary.com")

# --------------------------------------------------------------------------
# CORS — frontend origins (spec §10)
# --------------------------------------------------------------------------
CORS_ALLOWED_ORIGINS = config("CORS_ALLOWED_ORIGINS", default="http://localhost:5173", cast=Csv())
CORS_ALLOW_CREDENTIALS = False  # auth is a Bearer header, not cookies
CORS_EXPOSE_HEADERS = ["X-Request-ID", "Retry-After", "Content-Disposition"]  # spec 15 §11.1
# The web app sends its own X-Request-ID so a failure without a response still has a reference (spec 15 §4.10).
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*default_headers, "x-request-id")
CORS_PREFLIGHT_MAX_AGE = 3600

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Media (candidate photos, spec 03 §5.3). Local disk in development; the production
# provider (Cloudinary or S3-compatible, spec 03 §15.1) is a STORAGES setting change.
MEDIA_URL = config("MEDIA_URL", default="/media/")
MEDIA_ROOT = BASE_DIR / "media"
CANDIDATE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
CANDIDATE_PHOTO_MIN_SIDE = 400

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"},
                   "json": {"()": "apps.core.requestid.JsonFormatter"}},
    "filters": {
        "request_id": {"()": "apps.core.requestid.RequestIdFilter"},  # spec 15 §4.10
        "redact_pii": {"()": "apps.core.logging.RedactPII"},  # VIN/NIN never reach logs (spec 06 §9)
        "redact_query": {"()": "apps.core.logging.RedactSearchQuery"},  # search text never reaches logs (spec 12 §9)
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": config("LOG_FORMAT", default="text").replace("text", "simple"),
                             "filters": ["request_id", "redact_pii", "redact_query"]}},
    "root": {"handlers": ["console"], "level": "INFO"},
    # Django's request and dev-server loggers don't propagate to root by default; route them through the same filters.
    "loggers": {
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# No CSRF middleware on purpose: the API authenticates ONLY by Bearer header and
# deliberately ignores Clerk's __session cookie (apps/accounts/clerk.py), so there
# is no ambient browser credential for a cross-site request to ride on.
SILENCED_SYSTEM_CHECKS = ["security.W003"]

# --------------------------------------------------------------------------
# Donations (spec 05). Closed by default: see donations.services.donations_open().
# Secret keys live in .env only — never in code, never logged.
# --------------------------------------------------------------------------
FRONTEND_BASE_URL = config("FRONTEND_BASE_URL", default="http://localhost:5173")
DONATIONS_ENABLED = config("DONATIONS_ENABLED", default=False, cast=bool)
# Set only after legal sign-off on spec 05 §15 (FR-046).
DONATIONS_COMPLIANCE_APPROVED = config("DONATIONS_COMPLIANCE_APPROVED", default=False, cast=bool)
PAYMENT_PROVIDER = config("PAYMENT_PROVIDER", default="paystack")  # paystack | fake (fake: DEBUG/tests only)
PAYSTACK_SECRET_KEY = config("PAYSTACK_SECRET_KEY", default="")
PAYSTACK_API_BASE = config("PAYSTACK_API_BASE", default="https://api.paystack.co")  # only the fake server in live checks overrides it
# Optional Paystack webhook source allow-list (spec 15 §19.8: off by default; the signature is the control).
PAYSTACK_WEBHOOK_IPS = config("PAYSTACK_WEBHOOK_IPS", default="", cast=Csv())
PAYSTACK_CHANNELS = config("PAYSTACK_CHANNELS", default="card,bank,ussd,bank_transfer", cast=Csv())
FAKE_PAYMENTS_SECRET = config("FAKE_PAYMENTS_SECRET", default="fake-payments-dev-secret")
DONATION_MIN_KOBO = config("DONATION_MIN_KOBO", default=500_00, cast=int)  # ₦500
DONATION_MAX_PER_PAYMENT_KOBO = config("DONATION_MAX_PER_PAYMENT_KOBO", default=5_000_000_00, cast=int)  # ₦5m
# Electoral Act 2026 s. 92: ₦500m per individual/entity per candidate. Lower it if INEC sets a lower limit (s. 91).
DONATION_CANDIDATE_DONOR_CAP_KOBO = config("DONATION_CANDIDATE_DONOR_CAP_KOBO", default=500_000_000_00, cast=int)
DONATION_PARTY_DONOR_CAP_KOBO = config("DONATION_PARTY_DONOR_CAP_KOBO", default=500_000_000_00, cast=int)
DONATION_PRESETS_KOBO = [1_000_00, 5_000_00, 10_000_00, 50_000_00]

# --------------------------------------------------------------------------
# Membership + PVC verification (spec 06). Payments closed until the party/legal sign-off.
# --------------------------------------------------------------------------
MEMBERSHIP_ENABLED = config("MEMBERSHIP_ENABLED", default=False, cast=bool)
MEMBERSHIP_DATA_APPROVED = config("MEMBERSHIP_DATA_APPROVED", default=False, cast=bool)
MEMBERSHIP_FEE_KOBO = config("MEMBERSHIP_FEE_KOBO", default=2_000_000, cast=int)  # ₦20,000
# Official INEC portal for "Get your PVC" — empty hides the button until confirmed (spec 06 §15.2).
INEC_PVC_URL = config("INEC_PVC_URL", default="")
INEC_VOTER_CHECK_URL = "https://cvr.inecnigeria.org/vvs"  # officers' manual check (spec 06 §6.2)
# Fernet keys (comma-separated; first encrypts, all decrypt). Required in production.
PII_ENCRYPTION_KEYS = config("PII_ENCRYPTION_KEYS", default="", cast=Csv())
# Member photos live OUTSIDE the public media folder and are served only through authenticated views.
PRIVATE_MEDIA_ROOT = config("PRIVATE_MEDIA_ROOT", default=str(BASE_DIR / "private_media"))
# Spec 19 §4.3: a polling-unit file with fewer than 95% of the active units needs a second system admin, once at least
# this many units are loaded (so the 5-unit sample and test files aren't caught by it).
GEO_SECOND_ADMIN_MIN_UNITS = config("GEO_SECOND_ADMIN_MIN_UNITS", default=1000, cast=int)
# INEC's public polling-unit locator, linked from the finder (spec 19 §6.4). Official INEC site only.
INEC_PU_LOCATOR_URL = config("INEC_PU_LOCATOR_URL", default="https://cvr.inecnigeria.org/pu")

# --------------------------------------------------------------------------
# News and announcements (spec 07)
# --------------------------------------------------------------------------
# Electoral Act 2026: public campaigning ends 24 hours before polling day. Windows are scoped by office
# (INEC 2027 timetable; 08:00 poll opening assumed — confirm with INEC). Override with JSON in .env.
_DEFAULT_BLACKOUTS = [
    {"positions": ["president", "senator", "house_of_reps"], "start": "2027-01-15T08:00+01:00", "end": "2027-01-16T23:59+01:00"},
    {"positions": ["governor", "state_assembly"], "start": "2027-02-05T08:00+01:00", "end": "2027-02-06T23:59+01:00"},
]
CAMPAIGN_BLACKOUTS = json.loads(config("CAMPAIGN_BLACKOUTS", default=json.dumps(_DEFAULT_BLACKOUTS)))
# Spec 18 §4.4: campaign periods from INEC's 2027 timetable (Electoral Act 2026 s. 98). LGA offices: set per state (SIEC dates).
_DEFAULT_CAMPAIGN_PERIODS = [
    {"positions": ["president", "senator", "house_of_reps"], "start": "2026-08-19T00:00+01:00", "end": "2027-01-15T07:59+01:00"},
    {"positions": ["governor", "state_assembly"], "start": "2026-09-09T00:00+01:00", "end": "2027-02-05T07:59+01:00"},
]
CAMPAIGN_PERIODS = json.loads(config("CAMPAIGN_PERIODS", default=json.dumps(_DEFAULT_CAMPAIGN_PERIODS)))
NEWS_MAX_PENDING = 3
NEWS_MAX_PINNED = 3
NEWS_COMMUNITY_MIN_BODY = 100
NEWS_COMMUNITY_MAX_BODY = 5_000
NEWS_OFFICIAL_MAX_BODY = 20_000
CANDIDATE_POST_MAX_BODY = 10_000

# --------------------------------------------------------------------------
# Reporting and moderation (spec 13). Contact address shown on /rules for government agencies;
# blank hides the line. NDCConnect records acknowledgements; it doesn't send them (§15.3).
# --------------------------------------------------------------------------
MODERATION_LEGAL_EMAIL = config("MODERATION_LEGAL_EMAIL", default="")
# Copyright Act 2022: restore after a counter-notice the owner doesn't answer within this many days (§15.4).
COPYRIGHT_COUNTER_NOTICE_DAYS = config("COPYRIGHT_COUNTER_NOTICE_DAYS", default=10, cast=int)
COMPLIANCE_SAFETY_STATEMENT = config("COMPLIANCE_SAFETY_STATEMENT", default=(
    "Community rules R1, R2, R5 and R6 protect adults and children from harmful content. Reports on these rules are urgent; "
    "every decision is made by a person; removed content is labelled with the rule and can be appealed."))

# --------------------------------------------------------------------------
# Security and privacy (spec 14). Contacts are shown on /privacy and /security; blank shows "not set".
# --------------------------------------------------------------------------
PRIVACY_CONTACT_EMAIL = config("PRIVACY_CONTACT_EMAIL", default="")
SECURITY_CONTACT_EMAIL = config("SECURITY_CONTACT_EMAIL", default="")
CONTROLLER_NAME = config("CONTROLLER_NAME", default="NDC — Data Protection Officer")  # spec 14 §15.1
HOSTING_LOCATION = config("HOSTING_LOCATION", default="")  # spec 14 §15.6: blank = "to be confirmed"
# Admin roles need two-step verification (Clerk custom session claim `mfa` = {{user.two_factor_enabled}}). On in production.
ADMIN_MFA_REQUIRED = config("ADMIN_MFA_REQUIRED", default=False, cast=bool)
# Sensitive actions need a factor verified within this many minutes (Clerk `fva` claim).
REVERIFY_MINUTES = config("REVERIFY_MINUTES", default=10, cast=int)
REVERIFICATION_REQUIRED = config("REVERIFICATION_REQUIRED", default=True, cast=bool)
# Consent before political-opinion processing (spec 14 §3.2). Never turned off outside tests.
PRIVACY_CONSENT_REQUIRED = config("PRIVACY_CONSENT_REQUIRED", default=True, cast=bool)  # off only for older live checks
PRIVACY_REQUEST_DAYS = 30  # GAID 2025
RECORD_RETENTION_DAYS = 6 * 365  # audit log, consents, privacy requests and incidents (spec 14 §15.7)
ACCESS_REVIEW_DAYS = 90
ADMIN_INACTIVE_DAYS = 60
RESTORE_DRILL_MAX_DAYS = 100
DATA_UPLOAD_MAX_MEMORY_SIZE = 12 * 1024 * 1024  # spec 14 §4.9
CROSS_ORIGIN_RESOURCE_POLICY = config("CROSS_ORIGIN_RESOURCE_POLICY", default="same-site")
