"""Payment provider adapter interface — spec 05 §6.4."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class ProviderError(Exception):
    """The provider couldn't be reached or refused the request. No money moved because of it."""


class InvalidSignature(Exception):
    pass


@dataclass
class InitResult:
    checkout_url: str
    provider_reference: str = ""


@dataclass
class VerifyResult:
    # One of: pending, success, failed, abandoned, reversed
    status: str
    amount_kobo: int
    currency: str
    channel: str = ""
    paid_at: Optional[datetime] = None
    provider_reference: str = ""
    refunded_kobo: int = 0


@dataclass
class WebhookEvent:
    name: str
    reference: str
    payload_sha256: str


class PaymentProvider:
    name = ""

    def configured(self) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def initialize(self, donation, *, callback_url: str, base_url: str) -> InitResult:  # pragma: no cover
        raise NotImplementedError

    def verify(self, donation, *, check_refunds: bool = False) -> VerifyResult:  # pragma: no cover
        raise NotImplementedError

    def parse_webhook(self, raw_body: bytes, headers) -> WebhookEvent:  # pragma: no cover
        raise NotImplementedError
