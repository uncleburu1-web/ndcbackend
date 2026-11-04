"""
One webhook URL for every payment (spec 06 §6.3): /api/v1/webhooks/payments/<provider>/.
Events are signature-checked by the provider, then routed by reference prefix:
  NDC-  → donations     MEM-  → membership fees
Each app keeps its own records and status machine; they never share rows.
"""
import importlib
import logging

from .providers import get_provider
from .providers.base import InvalidSignature
from .providers.paystack import HANDLED_EVENTS as PAYSTACK_EVENTS

logger = logging.getLogger(__name__)

FAKE_EVENTS = {"charge.success", "charge.failed", "refund.processed"}
ROUTES = {"NDC-": "apps.donations.services", "MEM-": "apps.membership.services"}


def handler_for(reference: str):
    for prefix, module in ROUTES.items():
        if reference.startswith(prefix):
            return importlib.import_module(module)
    return None


def handle_webhook(provider_name: str, raw_body: bytes, headers) -> None:
    """Signature first; then one receipt per (event, reference, body) — Paystack sends no event ID or timestamp, so
    replays are harmless by design: the receipt de-duplicates and the owning app re-verifies with Paystack (spec 15 §6.5)."""
    from apps.integrations import webhooks as receipts
    from apps.integrations.models import WebhookReceipt

    provider = get_provider(provider_name)
    try:
        event = provider.parse_webhook(raw_body, headers)  # raises InvalidSignature
    except InvalidSignature:
        receipts.bad_signature(provider_name)
        raise
    event_id = receipts.body_hash(f"{event.name}|{event.reference}|".encode() + (raw_body or b""))
    receipt, is_new = receipts.receive(provider_name, event_id, event_type=event.name, subject=event.reference[:64], body=raw_body)
    if not is_new and receipt and receipt.status != WebhookReceipt.Status.FAILED:
        return
    handled = PAYSTACK_EVENTS if provider_name == "paystack" else FAKE_EVENTS
    module = handler_for(event.reference) if event.name in handled else None
    if module is None:
        if event.name in handled:
            logger.warning("Payment webhook %s for unknown reference %s", event.name, event.reference[:40])
        receipts.mark(receipt, WebhookReceipt.Status.IGNORED)
        return
    try:
        module.on_provider_event(provider_name, event.reference, event.name, event.payload_sha256)
    except Exception as exc:
        receipts.mark(receipt, WebhookReceipt.Status.FAILED, type(exc).__name__)
        from datetime import timedelta

        from apps.core import outbox

        outbox.enqueue("webhook_retry", {"receipt": str(receipt.pk)}, dedupe_key=f"webhook_retry:{receipt.pk}", delay=timedelta(minutes=1))
        raise
    receipts.mark(receipt, WebhookReceipt.Status.PROCESSED)


def sweep_all(*, older_than_minutes=10) -> dict:
    out = {}
    for prefix, module in ROUTES.items():
        mod = importlib.import_module(module)
        for k, v in mod.sweep(older_than_minutes=older_than_minutes).items():
            out[f"{prefix.rstrip('-').lower()}_{k}"] = v
    return out
