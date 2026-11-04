"""Webhook receipts (spec 15 §12.4): verify first, then `receive()`, then process; duplicates change nothing."""
import hashlib
import logging

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.requestid import current as current_request_id

from .models import WebhookReceipt

logger = logging.getLogger(__name__)
SIGNATURE_ALERT = 20  # bad signatures per provider per hour before one audit entry


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def receive(provider, event_id, *, event_type="", subject="", body=b"") -> tuple[WebhookReceipt | None, bool]:
    """Record the event. Returns (receipt, is_new); a duplicate returns (existing, False)."""
    try:
        with transaction.atomic():
            r = WebhookReceipt.objects.create(provider=provider, event_id=event_id[:128], event_type=event_type[:64], subject=subject[:64],
                                              body_sha256=body_hash(body), request_id=current_request_id())
            return r, True
    except IntegrityError:
        r = WebhookReceipt.objects.filter(provider=provider, event_id=event_id[:128]).first()
        return r, False


def mark(receipt, status, error=""):
    if receipt is None:
        return
    WebhookReceipt.objects.filter(pk=receipt.pk).update(status=status, error=error[:255], processed_at=timezone.now())


def bad_signature(provider, request=None):
    """Counted per provider per hour; more than SIGNATURE_ALERT writes one audit entry (a probing signal, as 14)."""
    key = f"int:{provider}:badsig:{timezone.now():%Y%m%d%H}"
    try:
        if cache.add(key, 1, 3700):
            n = 1
        else:
            n = cache.incr(key)
    except Exception:
        return
    if n == SIGNATURE_ALERT + 1:
        from apps.audit.models import AuditLog
        from apps.audit.services import record

        record(AuditLog.Action.SECURITY_WEBHOOK_SIGNATURE_FAILURES, actor_label=f"webhook:{provider}", request=request,
               provider=provider, count=n, window="1 hour")
