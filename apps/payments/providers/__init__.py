from django.conf import settings

from .base import InitResult, InvalidSignature, PaymentProvider, ProviderError, VerifyResult, WebhookEvent  # noqa: F401


def get_provider(name: str | None = None) -> PaymentProvider:
    """The configured provider, or a named one (webhooks / existing donations). Unknown → LookupError."""
    name = name or settings.PAYMENT_PROVIDER
    if name == "paystack":
        from .paystack import PaystackProvider

        return PaystackProvider()
    if name == "fake":
        from .fake import FakeProvider

        return FakeProvider()  # raises outside DEBUG/tests
    raise LookupError(name)
