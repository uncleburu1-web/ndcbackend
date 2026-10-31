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

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
