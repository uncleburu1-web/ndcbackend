"""
POST /api/v1/webhooks/clerk/ — spec §6.2.

- No auth; every request is verified with its Svix signature BEFORE any database write.
- Svix also rejects stale timestamps (replay window).
- Idempotent: the svix-id is recorded; repeats return 200 without reprocessing.
"""
import json
import logging

from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from svix.webhooks import Webhook, WebhookVerificationError

from . import services

logger = logging.getLogger(__name__)


class ClerkWebhookThrottle(AnonRateThrottle):
    scope = "clerk_webhook"


def _error(code, message, http_status):
    return Response({"error": {"status": http_status, "code": code, "message": message, "fields": {}}}, status=http_status)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ClerkWebhookThrottle])
def clerk_webhook(request):
    secret = settings.CLERK_WEBHOOK_SIGNING_SECRET
    if not secret:
        logger.error("Clerk webhook received but CLERK_WEBHOOK_SIGNING_SECRET is not set.")
        return _error("webhook_not_configured", "Webhook is not configured.", status.HTTP_503_SERVICE_UNAVAILABLE)

    headers = {
        "svix-id": request.headers.get("svix-id", ""),
        "svix-timestamp": request.headers.get("svix-timestamp", ""),
        "svix-signature": request.headers.get("svix-signature", ""),
    }
    if not all(headers.values()):
        return _error("invalid_signature", "Missing webhook signature headers.", status.HTTP_400_BAD_REQUEST)

    body = request.body  # raw bytes — must be verified exactly as received
    try:
        Webhook(secret).verify(body, headers)
    except WebhookVerificationError:
        from apps.integrations.webhooks import bad_signature

        bad_signature("clerk", request)
        return _error("invalid_signature", "Webhook signature could not be verified.", status.HTTP_400_BAD_REQUEST)

    try:
        event = json.loads(body)
        event_type = event["type"]
        data = event["data"]
    except (ValueError, KeyError, TypeError):
        return _error("invalid_payload", "Webhook payload is not a Clerk event.", status.HTTP_400_BAD_REQUEST)

    from apps.integrations import webhooks as receipts
    from apps.integrations.models import WebhookReceipt

    subject = str((data or {}).get("id") or "")[:64] if isinstance(data, dict) else ""
    receipt, is_new = receipts.receive("clerk", headers["svix-id"], event_type=event_type, subject=subject, body=body)
    if not is_new and receipt and receipt.status != WebhookReceipt.Status.FAILED:
        return Response({"received": True, "duplicate": True}, status=status.HTTP_200_OK)
    try:
        with transaction.atomic():
            handled = True
            if event_type in ("user.created", "user.updated"):
                services.upsert_from_clerk(data, source="clerk:webhook")
            elif event_type == "user.deleted":
                if data.get("id"):
                    services.mark_deleted(data["id"])
            else:
                handled = False  # unhandled event types are acknowledged and ignored
    except Exception as exc:
        logger.exception("Clerk webhook processing failed")
        receipts.mark(receipt, WebhookReceipt.Status.FAILED, type(exc).__name__)
        from apps.core import outbox

        outbox.enqueue("webhook_retry", {"receipt": str(receipt.pk)}, dedupe_key=f"webhook_retry:{receipt.pk}",
                       delay=__import__("datetime").timedelta(minutes=1))
        return _error("server_error", "Webhook could not be processed.", status.HTTP_500_INTERNAL_SERVER_ERROR)
    receipts.mark(receipt, WebhookReceipt.Status.PROCESSED if handled else WebhookReceipt.Status.IGNORED)
    return Response({"received": True}, status=status.HTTP_200_OK)
