"""
Every external service NDCConnect talks to (spec 15 §12.1). Names of settings only, never values.
Readiness (11) shows an Integrations group from this list and /admin/integrations shows the detail.
Adding a service means adding it here, to the data register's processors (14) and to spec 15 §3.2.
"""
from dataclasses import dataclass, field

from django.conf import settings

from . import http


@dataclass(frozen=True)
class Integration:
    key: str
    name: str
    purpose: str
    settings: tuple
    adapter: str
    timeouts: str
    data_sent: str
    processor: str  # the data register's processor name (apps/privacy/register.py)
    in_use: callable = field(default=lambda: True)
    configured: callable = field(default=lambda: True)


def _clerk_configured():
    return bool(settings.CLERK_SECRET_KEY or settings.CLERK_JWT_KEY)


def _paystack_in_use():
    return settings.PAYMENT_PROVIDER == "paystack"


def _paystack_configured():
    return (settings.PAYSTACK_SECRET_KEY or "").startswith(("sk_test_", "sk_live_"))


def _cloudinary_in_use():
    return getattr(settings, "MEDIA_BACKEND", "local") == "cloudinary"


def _cloudinary_configured():
    from apps.media.cloudinary_api import configured

    return configured()


INTEGRATIONS = [
    Integration("clerk", "Clerk", "Sign-in, sessions and account bans",
                ("CLERK_SECRET_KEY", "CLERK_JWT_KEY", "CLERK_WEBHOOK_SIGNING_SECRET", "CLERK_AUTHORIZED_PARTIES"),
                "apps/accounts/clerk.py", "connect 3 s, read 5 s", "Clerk user ID; ban, unban and delete requests", "Clerk",
                configured=_clerk_configured),
    Integration("paystack", "Paystack", "Donations and membership fees (hosted checkout)",
                ("PAYSTACK_SECRET_KEY",), "apps/payments/providers/paystack.py", "connect 3 s, read 10 s",
                "Payer email, amount, reference, callback URL", "Paystack", in_use=_paystack_in_use, configured=_paystack_configured),
    Integration("cloudinary", "Cloudinary", "Public images: candidate photos, post, news and announcement images",
                ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET", "CLOUDINARY_UPLOAD_PRESET", "CLOUDINARY_FOLDER"),
                "apps/media/cloudinary_api.py", "connect 3 s, read 10 s (30 s for server uploads)",
                "Image files (metadata stripped), random public IDs", "Cloudinary", in_use=_cloudinary_in_use,
                configured=_cloudinary_configured),
]


def by_key(key):
    return next((i for i in INTEGRATIONS if i.key == key), None)


def state(i: Integration) -> str:
    """ok | unavailable | not_configured | not_in_use — words for people are the frontend's."""
    if not i.in_use():
        return "not_in_use"
    if not i.configured():
        return "not_configured"
    h = http.health(i.key)
    if h["circuit_open"]:
        return "unavailable"
    if h["last_failure"] and (not h["last_success"] or h["last_failure"] > h["last_success"]):
        return "failing"
    return "ok"


def snapshot() -> list[dict]:
    out = []
    for i in INTEGRATIONS:
        h = http.health(i.key)
        out.append({"key": i.key, "name": i.name, "purpose": i.purpose, "settings": list(i.settings), "timeouts": i.timeouts,
                    "data_sent": i.data_sent, "in_use": i.in_use(), "configured": i.configured(), "state": state(i),
                    "last_success": h["last_success"], "last_failure": h["last_failure"], "last_reason": h["last_reason"],
                    "circuit_open": h["circuit_open"]})
    return out
