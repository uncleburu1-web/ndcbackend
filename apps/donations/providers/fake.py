"""
Fake provider — DEBUG and tests only (spec 05 §6.4). Refuses to load anywhere else.

Its "bank" is a cache entry per reference, so the dev server and management
commands share it (file cache in dev settings). The dev checkout page
(/api/v1/dev/fake-checkout/<ref>/) offers Pay / Fail / Pay-without-webhook and
sends a correctly signed webhook, so the real webhook + verify path runs.
"""
import hashlib
import hmac
import json

from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .base import InitResult, InvalidSignature, PaymentProvider, VerifyResult, WebhookEvent

SIGNATURE_HEADER = "x-fake-signature"


def fake_allowed() -> bool:
    return bool(settings.DEBUG or getattr(settings, "TESTING", False))


def _bank():
    return caches["fake_payments"] if "fake_payments" in settings.CACHES else caches["default"]


def bank_get(reference: str) -> dict | None:
    return _bank().get(f"fake:{reference}")


def bank_set(reference: str, **state) -> dict:
    current = bank_get(reference) or {}
    current.update(state)
    _bank().set(f"fake:{reference}", current, timeout=7 * 24 * 3600)
    return current


def sign(raw_body: bytes) -> str:
    return hmac.new(settings.FAKE_PAYMENTS_SECRET.encode(), raw_body, hashlib.sha512).hexdigest()


class FakeProvider(PaymentProvider):
    name = "fake"

    def __init__(self):
        if not fake_allowed():
            raise ImproperlyConfigured("The fake payment provider only runs with DEBUG or in tests.")

    def configured(self) -> bool:
        return True

    def initialize(self, donation, *, callback_url: str, base_url: str) -> InitResult:
        bank_set(donation.reference, status="pending", amount_kobo=donation.amount_kobo, currency="NGN", channel="",
                 paid_at=None, id=f"fake_{donation.reference[-8:]}", refunded_kobo=0, callback_url=callback_url)
        return InitResult(checkout_url=f"{base_url.rstrip('/')}/api/v1/dev/fake-checkout/{donation.reference}/",
                          provider_reference=f"fake_{donation.reference[-8:]}")

    def verify(self, donation, *, check_refunds: bool = False) -> VerifyResult:
        s = bank_get(donation.reference) or {"status": "abandoned", "amount_kobo": donation.amount_kobo, "currency": "NGN"}
        return VerifyResult(status=s["status"], amount_kobo=int(s["amount_kobo"]), currency=s.get("currency", "NGN"),
                            channel=s.get("channel", ""), paid_at=s.get("paid_at"), provider_reference=s.get("id", ""),
                            refunded_kobo=int(s.get("refunded_kobo", 0)))

    def parse_webhook(self, raw_body: bytes, headers) -> WebhookEvent:
        signature = headers.get(SIGNATURE_HEADER, "")
        if not signature or not hmac.compare_digest(sign(raw_body), signature):
            raise InvalidSignature()
        try:
            event = json.loads(raw_body)
            ref = (event.get("data") or {}).get("reference", "")
        except (ValueError, AttributeError):
            raise InvalidSignature() from None
        return WebhookEvent(name=event.get("event", ""), reference=str(ref), payload_sha256=hashlib.sha256(raw_body).hexdigest())

    # helpers for the dev page and tests
    @staticmethod
    def settle(reference: str, outcome: str, *, amount_kobo: int | None = None, currency: str = "NGN", channel: str = "card"):
        state = {"status": outcome, "currency": currency, "channel": channel if outcome == "success" else ""}
        if outcome == "success":
            state["paid_at"] = timezone.now()
        if amount_kobo is not None:
            state["amount_kobo"] = amount_kobo
        return bank_set(reference, **state)

    @staticmethod
    def webhook_body(event: str, reference: str) -> bytes:
        return json.dumps({"event": event, "data": {"reference": reference}}).encode()
