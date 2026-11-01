"""
One webhook URL for every payment (spec 06 §6.3): /api/v1/webhooks/payments/<provider>/.
Events are signature-checked by the provider, then routed by reference prefix:
  NDC-  → donations     MEM-  → membership fees
Each app keeps its own records and status machine; they never share rows.
"""
import importlib
import logging

from .providers import get_provider
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
    provider = get_provider(provider_name)
    event = provider.parse_webhook(raw_body, headers)  # raises InvalidSignature
    handled = PAYSTACK_EVENTS if provider_name == "paystack" else FAKE_EVENTS
    if event.name not in handled:
        return
    module = handler_for(event.reference)
    if module is None:
        logger.warning("Payment webhook %s for unknown reference %s", event.name, event.reference[:40])
        return
    module.on_provider_event(provider_name, event.reference, event.name, event.payload_sha256)


def sweep_all(*, older_than_minutes=10) -> dict:
    out = {}
    for prefix, module in ROUTES.items():
        mod = importlib.import_module(module)
        for k, v in mod.sweep(older_than_minutes=older_than_minutes).items():
            out[f"{prefix.rstrip('-').lower()}_{k}"] = v
    return out
