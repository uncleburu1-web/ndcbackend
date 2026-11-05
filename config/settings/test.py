from .base import *  # noqa: F401,F403
from .base import LOGGING, REST_FRAMEWORK

DEBUG = False
CLERK_SECRET_KEY = ""
CLERK_JWT_KEY = ""  # tests inject a generated key with override_settings
CLERK_WEBHOOK_SIGNING_SECRET = "whsec_dGVzdC1zZWNyZXQtZm9yLW5kY2Nvbm5lY3QtdGVzdHM="
CLERK_AUTHORIZED_PARTIES = ["http://localhost:5173"]
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {k: "10000/min" for k in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]}}  # every scope, generous
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
          "fake_payments": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "fake-payments"}}
TESTING = True
PAYMENT_PROVIDER = "fake"
FRONTEND_BASE_URL = "http://localhost:5173"
LOGGING = {**LOGGING, "loggers": {"django.request": {"level": "ERROR"}, "apps.core.requests": {"level": "ERROR"},
                                  "apps.integrations": {"level": "ERROR"}, "apps.core.throttling": {"level": "ERROR"}}}
# Test-only Fernet key (never used anywhere real).
PII_ENCRYPTION_KEYS = ["n_Y65S7dmM7W529y6AmuOOgeSHUaayG9MMPgxNXWi-4="]
import tempfile as _tempfile  # noqa: E402
PRIVATE_MEDIA_ROOT = _tempfile.mkdtemp(prefix="ndc-private-test-")

# Consent hooks are exercised by the privacy tests with override_settings; elsewhere fixtures needn't grant them.
PRIVACY_CONSENT_REQUIRED = False

MEDIA_BACKEND = "local"
CLOUDINARY_CLOUD_NAME = ""
CLOUDINARY_API_KEY = ""
CLOUDINARY_API_SECRET = ""
CLOUDINARY_FOLDER = "ndcconnect/test"
