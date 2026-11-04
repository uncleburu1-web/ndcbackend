"""
Paystack adapter — spec 05 §6.4. Hosted checkout only; NDCConnect never sees card data.

- Initialize:  POST /transaction/initialize  (amount in kobo, NGN)
- Verify:      GET  /transaction/verify/:reference   (statuses: success, failed, abandoned,
               ongoing/pending/processing/queued, reversed)
- Refunds:     GET  /refund?transaction=<transaction id>   (sum of `processed` refunds)
- Webhooks:    x-paystack-signature = hex HMAC-SHA512 of the RAW body, keyed with the secret key.

The secret key comes from settings (backend .env only) and is never logged.
"""
import hashlib
import hmac
import json

from django.conf import settings
from django.utils.dateparse import parse_datetime

from .base import InitResult, InvalidSignature, PaymentProvider, ProviderError, VerifyResult, WebhookEvent

PENDING_STATES = {"ongoing", "pending", "processing", "queued", "send_birthday", "send_otp"}
HANDLED_EVENTS = {"charge.success", "refund.processed", "refund.failed", "refund.pending", "refund.processing"}


class PaystackProvider(PaymentProvider):
    name = "paystack"

    def _key(self) -> str:
        return settings.PAYSTACK_SECRET_KEY or ""

    def configured(self) -> bool:
        return self._key().startswith(("sk_test_", "sk_live_"))

    def _call(self, method: str, path: str, body=None) -> dict:
        """Through the shared client (spec 15 §12.2): 3 s connect, 10 s read, breaker; retries only for GETs."""
        from apps.integrations import http

        op = path.split("?")[0].rsplit("/", 1)[0] if path.startswith("/transaction/verify/") else path.split("?")[0]
        try:
            res = http.call("paystack", method, settings.PAYSTACK_API_BASE + path, operation=op, json=body,
                            headers={"Authorization": f"Bearer {self._key()}", "Accept": "application/json"},
                            idempotent=method == "GET")
        except http.ProviderUnavailable as exc:
            raise ProviderError(f"Paystack unreachable: {exc.reason}") from None
        data = res.json()
        if res.status >= 400:
            # Never include the request (it carries the key) in the error.
            raise ProviderError(f"Paystack {path.split('?')[0]} returned {res.status}")
        if not isinstance(data, dict) or not data.get("status"):
            raise ProviderError(f"Paystack {path.split('?')[0]} refused the request")
        return data.get("data") or {}

    def initialize(self, payable, *, callback_url: str, base_url: str) -> InitResult:
        data = self._call("POST", "/transaction/initialize", {
            "amount": payable.amount_kobo,
            "email": payable.payer_email,
            "currency": "NGN",
            "reference": payable.reference,
            "callback_url": callback_url,
            "channels": list(settings.PAYSTACK_CHANNELS),
            "metadata": payable.metadata,
        })
        return InitResult(checkout_url=data["authorization_url"], provider_reference="")

    def verify(self, payable, *, check_refunds: bool = False) -> VerifyResult:
        data = self._call("GET", f"/transaction/verify/{payable.reference}")
        raw = (data.get("status") or "").lower()
        status = "pending" if raw in PENDING_STATES else raw
        if status not in {"pending", "success", "failed", "abandoned", "reversed"}:
            status = "pending"
        tx_id = str(data.get("id") or "")
        refunded = 0
        if tx_id and (check_refunds or status == "reversed"):
            refunds = self._call("GET", f"/refund?transaction={tx_id}")
            refunded = sum(int(r.get("amount") or 0) for r in (refunds if isinstance(refunds, list) else [])
                           if (r.get("status") or "").lower() == "processed")
        paid_at = parse_datetime(data["paid_at"]) if data.get("paid_at") else None
        return VerifyResult(status=status, amount_kobo=int(data.get("amount") or 0), currency=(data.get("currency") or "").upper(),
                            channel=data.get("channel") or "", paid_at=paid_at, provider_reference=tx_id, refunded_kobo=refunded)

    def parse_webhook(self, raw_body: bytes, headers) -> WebhookEvent:
        signature = headers.get("x-paystack-signature", "")
        expected = hmac.new(self._key().encode(), raw_body, hashlib.sha512).hexdigest()
        if not self.configured() or not signature or not hmac.compare_digest(expected, signature):
            raise InvalidSignature()
        try:
            event = json.loads(raw_body)
            data = event.get("data") or {}
            ref = data.get("reference") or data.get("transaction_reference") or ""
        except (ValueError, AttributeError):
            raise InvalidSignature() from None
        return WebhookEvent(name=event.get("event", ""), reference=str(ref), payload_sha256=hashlib.sha256(raw_body).hexdigest())

    def health(self) -> None:
        """For check_donations: proves the key works (401 → ProviderError)."""
        self._call("GET", "/transaction?perPage=1")
