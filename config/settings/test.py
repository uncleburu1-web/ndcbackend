from .base import *  # noqa: F401,F403
from .base import LOGGING, REST_FRAMEWORK

DEBUG = False
CLERK_SECRET_KEY = ""
CLERK_JWT_KEY = ""  # tests inject a generated key with override_settings
CLERK_WEBHOOK_SIGNING_SECRET = "whsec_dGVzdC1zZWNyZXQtZm9yLW5kY2Nvbm5lY3QtdGVzdHM="
CLERK_AUTHORIZED_PARTIES = ["http://localhost:5173"]
REST_FRAMEWORK = {**REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {"profile_write": "10000/min", "clerk_webhook": "10000/min", "candidates_public": "10000/min", "follows": "10000/min", "donations_start": "10000/min", "donations_receipt": "10000/min", "finance_public": "10000/min", "payment_webhook": "10000/min", "pvc_submit": "10000/min", "membership_pay": "10000/min", "pii_reveal": "10000/min", "news_public": "10000/min", "news_submit": "10000/min", "news_write": "10000/min", "news_report": "10000/min", "posts_write": "10000/min", "admin_write": "10000/min", "admin_read": "10000/min", "search_public": "10000/min"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
          "fake_payments": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "fake-payments"}}
TESTING = True
PAYMENT_PROVIDER = "fake"
FRONTEND_BASE_URL = "http://localhost:5173"
LOGGING = {**LOGGING, "loggers": {"django.request": {"level": "ERROR"}}}
# Test-only Fernet key (never used anywhere real).
PII_ENCRYPTION_KEYS = ["n_Y65S7dmM7W529y6AmuOOgeSHUaayG9MMPgxNXWi-4="]
import tempfile as _tempfile  # noqa: E402
PRIVATE_MEDIA_ROOT = _tempfile.mkdtemp(prefix="ndc-private-test-")
