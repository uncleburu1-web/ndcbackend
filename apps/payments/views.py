"""Payment webhook and the DEBUG-only fake checkout page — shared by donations and membership (spec 06 §6.3)."""
import json
import logging

from django.http import HttpResponse, HttpResponseNotFound, HttpResponseRedirect
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from rest_framework.throttling import AnonRateThrottle

from . import dispatch
from .providers import InvalidSignature
from .providers.fake import SIGNATURE_HEADER, FakeProvider, bank_get, fake_allowed, sign

logger = logging.getLogger(__name__)


class WebhookThrottle(AnonRateThrottle):
    scope = "payment_webhook"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _DRFLike:
    """Just enough of a DRF request for the throttle's IP lookup."""

    def __init__(self, request):
        self._request = request
        self.META = request.META
        self.user = None


def _json_error(code, err, message):
    from apps.core.requestid import current

    return HttpResponse(json.dumps({"error": {"status": code, "code": err, "message": message, "fields": {}, "request_id": current()}}),
                        status=code, content_type="application/json")


@csrf_exempt
def payment_webhook(request, provider):
    """POST /webhooks/payments/<provider>/ — signature first; the owning app verifies with the provider."""
    if request.method != "POST":
        return HttpResponse(status=405)
    if provider not in ("paystack", "fake") or (provider == "fake" and not fake_allowed()):
        return HttpResponseNotFound()
    throttle = WebhookThrottle()
    if not throttle.allow_request(_DRFLike(request), None):
        return HttpResponse(status=429)
    from django.conf import settings

    allow = [ip.strip() for ip in getattr(settings, "PAYSTACK_WEBHOOK_IPS", []) if ip.strip()]
    if provider == "paystack" and allow and throttle.get_ident(_DRFLike(request)) not in allow:  # optional, spec 15 §19.8
        logger.warning("Paystack webhook from an address outside PAYSTACK_WEBHOOK_IPS")
        return _json_error(403, "permission_denied", "Webhook source not allowed.")
    try:
        dispatch.handle_webhook(provider, request.body, request.headers)
    except InvalidSignature:
        return _json_error(400, "invalid_signature", "Webhook signature could not be verified.")
    except Exception:
        logger.exception("Payment webhook processing failed")
        return _json_error(500, "server_error", "Webhook could not be processed.")  # provider retries
    return HttpResponse(status=200)


@csrf_exempt
def fake_checkout(request, reference):
    """DEBUG/tests only: stand-in for the provider's hosted page, for NDC- and MEM- references."""
    if not fake_allowed():
        return HttpResponseNotFound()
    state = bank_get(reference)
    if state is None or dispatch.handler_for(reference) is None:
        return HttpResponseNotFound()
    if request.method == "POST":
        outcome = request.POST.get("outcome")
        if outcome in ("success", "failed"):
            FakeProvider.settle(reference, outcome)
            body = FakeProvider.webhook_body("charge.success" if outcome == "success" else "charge.failed", reference)
            dispatch.handle_webhook("fake", body, {SIGNATURE_HEADER: sign(body)})
        elif outcome == "success_no_webhook":
            FakeProvider.settle(reference, "success")  # bank says paid; webhook "lost"
        return HttpResponseRedirect(state["callback_url"])
    naira = f"₦{int(state['amount_kobo']) // 100:,}"
    what = "membership fee" if reference.startswith("MEM-") else "donation"
    return HttpResponse(f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Fake checkout</title>
<body style="font-family:system-ui;max-width:28rem;margin:3rem auto;padding:0 1rem">
<p style="background:#fff3cd;padding:.5rem 1rem">Development only: no real money moves.</p>
<h1>Pay {escape(naira)}</h1><p>{escape(what.capitalize())} · reference {escape(reference)}</p>
<form method="post"><button name="outcome" value="success">Pay</button>
<button name="outcome" value="failed">Fail</button>
<button name="outcome" value="success_no_webhook">Pay (webhook lost)</button>
<button name="outcome" value="leave">Leave (stays pending)</button></form></body>""")
