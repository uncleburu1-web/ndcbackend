"""Request IDs (spec 15 §4.10) and request timing (§12.6).

Every request gets an ID: a valid inbound `X-Request-ID` from the proxy is kept, anything else is replaced by a UUID4.
It is returned in the response header, added to error envelopes, attached to every log record and stored on outbox
jobs and webhook receipts created during the request.
"""
import contextvars
import logging
import re
import time
import uuid

from django.conf import settings

_current = contextvars.ContextVar("request_id", default="")
VALID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
logger = logging.getLogger("apps.core.requests")


def current() -> str:
    return _current.get()


def set_current(value: str):
    return _current.set(value)


def new_id() -> str:
    return uuid.uuid4().hex


class RequestIdMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get("HTTP_X_REQUEST_ID", "")
        rid = incoming if VALID.match(incoming or "") else new_id()
        request.request_id = rid
        token = _current.set(rid)
        started = time.monotonic()
        try:
            response = self.get_response(request)
        finally:
            _current.reset(token)
        response["X-Request-ID"] = rid
        _stamp_envelope(response, rid)
        ms = int((time.monotonic() - started) * 1000)
        match = getattr(request, "resolver_match", None)
        route = (match.url_name or match.route) if match else "unresolved"
        slow = ms >= getattr(settings, "SLOW_REQUEST_MS", 1500)
        if request.path.startswith("/api/"):
            # route name, never the raw path (IDs) or query string (search text, spec 12)
            (logger.warning if slow else logger.info)("%s %s %s %dms%s", request.method, route, response.status_code, ms,
                                                       " slow" if slow else "", extra={"request_id": rid, "route": route,
                                                                                         "status": response.status_code, "ms": ms})
        return response


def _stamp_envelope(response, rid):
    """Hand-built error envelopes (webhooks, a few views) get the request ID too."""
    if response.status_code < 400 or "json" not in response.get("Content-Type", "") or getattr(response, "streaming", False):
        return
    import json

    try:
        data = json.loads(response.content or b"null")
    except ValueError:
        return
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict) and not err.get("request_id"):
        err["request_id"] = rid
        response.content = json.dumps(data).encode()
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))


class RequestIdFilter(logging.Filter):
    """Adds `request_id` to every record (empty outside a request)."""

    def filter(self, record):
        if not getattr(record, "request_id", ""):
            record.request_id = _current.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """LOG_FORMAT=json: one JSON object per line. Only the (already redacted) message and fixed fields."""

    def format(self, record):
        import json

        out = {"time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname, "logger": record.name,
               "request_id": getattr(record, "request_id", "-"), "message": record.getMessage()}
        for k in ("route", "status", "ms", "provider", "operation", "duration_ms"):
            if hasattr(record, k):
                out[k] = getattr(record, k)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)
