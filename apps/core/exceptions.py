"""
One error envelope everywhere (spec §6.3, NFR-013):

    {"error": {"status": 400, "code": "validation_error", "message": "…", "fields": {"phone_number": ["…"]}}}

Unhandled exceptions return a generic 500 with no internal details (NFR-010).
"""
import logging

from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class ServiceError(exceptions.APIException):
    """Raise from service code with a stable machine-readable code."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "error"
    default_detail = "The request could not be completed."

    def __init__(self, message=None, code=None, status_code=None):
        if status_code:
            self.status_code = status_code
        super().__init__(detail=message or self.default_detail, code=code or self.default_code)


def _first(data):
    if isinstance(data, list) and data:
        return _first(data[0])
    if isinstance(data, dict) and data:
        return _first(next(iter(data.values())))
    return str(data)


def _request_id(context):
    from .requestid import current

    req = context.get("request")
    return getattr(getattr(req, "_request", req), "request_id", "") or current()


def _translated(message):
    """Spec 21 §6: the message in the request's language when the catalogue has it (msgid = the English text)."""
    from django.utils import translation

    if not isinstance(message, str) or translation.get_language() in (None, "en", "en-us"):
        return message
    return translation.gettext(message)


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    rid = _request_id(context)
    if response is None:
        logger.exception("Unhandled API error", exc_info=exc)
        return Response(
            {"error": {"status": 500, "code": "server_error",
                       "message": f"Something went wrong on our side. Reference: {rid[:8]}" if rid else "Something went wrong on our side.",
                       "fields": {}, "request_id": rid}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    data = response.data
    fields = {}
    if isinstance(exc, exceptions.ValidationError) and isinstance(data, dict):
        fields = {k: v for k, v in data.items() if k != "non_field_errors"}
        non_field = data.get("non_field_errors")
        message = _first(non_field) if non_field else "Please correct the highlighted fields."
        code = "validation_error"
        # A validation error may carry its own stable code/message (e.g. application_incomplete, spec 06 §7.3).
        code = getattr(exc, "envelope_code", code)
        message = getattr(exc, "envelope_message", message)
    elif isinstance(exc, exceptions.Throttled):
        wait = int(exc.wait or 60)
        message, code = f"Too many tries. Try again in {wait} seconds.", "throttled"
        response["Retry-After"] = str(wait)  # spec 15 §11.4
    else:
        detail = data.get("detail", data) if isinstance(data, dict) else data
        message = _first(detail)
        codes = exc.get_codes() if isinstance(exc, exceptions.APIException) else "error"
        code = codes if isinstance(codes, str) else _first(codes)
        # DRF's NotAuthenticated / AuthenticationFailed → the spec's stable code.
        if isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)) and code in ("not_authenticated", "authentication_failed"):
            code, message = "not_authenticated", "Sign in to continue."

    message, fields = _translated(message), {k: [_translated(m) for m in v] if isinstance(v, list) else v for k, v in fields.items()}
    response.data = {"error": {"status": response.status_code, "code": code, "message": message, "fields": fields, "request_id": rid}}
    if isinstance(exc, exceptions.Throttled):
        response.data["error"]["retry_after"] = int(exc.wait or 60)
    from .errors import CATALOGUE

    if code not in CATALOGUE:
        logger.warning("Error code %s is not in the catalogue (spec 15 §4.5)", code)
    request = context.get("request")
    if response.status_code == 403 and request is not None and request.path.startswith("/api/v1/admin/"):
        from apps.access.security import note_admin_denied

        try:
            note_admin_denied(request)
        except Exception:  # pragma: no cover - never break an error response
            logger.exception("Couldn't count a denied admin request")
    extra = getattr(exc, "extra", None)  # e.g. {"until": …} for publishing_paused (spec 07 §7.6)
    if isinstance(extra, dict):
        response.data["error"].update({k: v for k, v in extra.items() if k not in response.data["error"]})
    return response
