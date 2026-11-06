"""Production hardening (NFR-001)."""
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import CLERK_AUTHORIZED_PARTIES, CLERK_JWT_KEY, CLERK_SECRET_KEY, CLERK_WEBHOOK_SIGNING_SECRET, SECRET_KEY, config

DEBUG = False

for name, value in {
    "DJANGO_SECRET_KEY": not SECRET_KEY.startswith("insecure"),
    "CLERK_SECRET_KEY or CLERK_JWT_KEY": bool(CLERK_SECRET_KEY or CLERK_JWT_KEY),
    "CLERK_WEBHOOK_SIGNING_SECRET": bool(CLERK_WEBHOOK_SIGNING_SECRET),
    "CLERK_AUTHORIZED_PARTIES": bool(CLERK_AUTHORIZED_PARTIES),
}.items():
    if not value:
        raise ImproperlyConfigured(f"Set {name} in production.")

from .base import PAYMENT_PROVIDER, PII_ENCRYPTION_KEYS  # noqa: E402

if not [k for k in PII_ENCRYPTION_KEYS if k.strip()]:
    raise ImproperlyConfigured("Set PII_ENCRYPTION_KEYS in production (VIN/NIN encryption, spec 06 §9).")

if PAYMENT_PROVIDER == "fake":
    raise ImproperlyConfigured("The fake payment provider can't run in production. Set PAYMENT_PROVIDER=paystack.")

# Railway (deployment ahead of spec 19): it sets RAILWAY_* variables, serves the app on RAILWAY_PUBLIC_DOMAIN, and probes the
# health check over plain HTTP with the Host "healthcheck.railway.app". Liveness carries no data, so it skips the HTTPS redirect.
if config("RAILWAY_ENVIRONMENT_NAME", default=""):
    _railway_domain = config("RAILWAY_PUBLIC_DOMAIN", default="")
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "healthcheck.railway.app", *([_railway_domain] if _railway_domain else [])]  # noqa: F405
SECURE_REDIRECT_EXEMPT = [r"^api/v1/health/$"]

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"  # spec 14 §4.9
X_FRAME_OPTIONS = "DENY"

# Spec 14 §3.4: every admin role uses two-step verification in production (needs Clerk Pro + the `mfa` session claim).
ADMIN_MFA_REQUIRED = config("ADMIN_MFA_REQUIRED", default=True, cast=bool)
