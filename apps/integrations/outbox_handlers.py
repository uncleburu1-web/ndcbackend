"""Retry a failed webhook from what the receipt keeps (an ID, never the payload)."""
from apps.core.outbox import handler

from .models import WebhookReceipt
from .webhooks import mark


@handler("webhook_retry")
def webhook_retry(payload):
    r = WebhookReceipt.objects.get(pk=payload["receipt"])
    if r.status == WebhookReceipt.Status.PROCESSED:
        return
    WebhookReceipt.objects.filter(pk=r.pk).update(attempts=r.attempts + 1)
    if r.provider == "clerk":
        from apps.accounts import clerk, services

        if r.event_type == "user.deleted":
            services.mark_deleted(r.subject)
        else:
            data = clerk.fetch_user(r.subject)
            if data is None:
                raise RuntimeError("Clerk user could not be fetched")
            services.upsert_from_clerk(data, source="clerk:webhook")
    else:
        from apps.payments import dispatch

        module = dispatch.handler_for(r.subject)
        if module is None:
            mark(r, WebhookReceipt.Status.IGNORED)
            return
        module.on_provider_event(r.provider, r.subject, r.event_type, r.body_sha256)
    mark(r, WebhookReceipt.Status.PROCESSED)
