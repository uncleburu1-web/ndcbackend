"""
The shared outbound HTTP client (spec 15 §12.2). Every call to an external service goes through `call()` (raw HTTP)
or `guarded()` (around an SDK call), so each provider gets:
- explicit timeouts (connect 3 s, read per provider);
- retries only for idempotent calls, with backoff and jitter;
- a circuit breaker in the shared cache: 5 failures within 60 s open it for 30 s, then one trial call;
- logging of provider, operation, status and duration with the request ID, never keys or bodies;
- health (last success and failure) for readiness and /admin/integrations.
"""
import http.client
import json as _json
import logging
import random
import ssl
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass

from django.core.cache import cache
from django.utils import timezone

from apps.core.requestid import current as current_request_id

logger = logging.getLogger("apps.integrations")

CONNECT_TIMEOUT = 3
READ_TIMEOUTS = {"paystack": 10, "clerk": 5, "cloudinary": 10, "cloudinary_upload": 30}
FAILURE_WINDOW, FAILURE_LIMIT, OPEN_SECONDS = 60, 5, 30


class ProviderUnavailable(Exception):
    """The provider can't be reached, timed out, or its circuit is open. Callers map this to their 50x error."""

    def __init__(self, provider, reason):
        self.provider, self.reason = provider, reason
        super().__init__(f"{provider} unavailable: {reason}")


@dataclass
class HttpResult:
    status: int
    body: bytes
    headers: dict

    def json(self):
        try:
            return _json.loads(self.body or b"null")
        except ValueError:
            return None


def _k(provider, what):
    return f"int:{provider}:{what}"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # the cache must never break a provider call (spec 15 §11.4)
        logger.warning("Integration cache unavailable", exc_info=False)
        return default


def is_open(provider) -> bool:
    return bool(_safe(lambda: cache.get(_k(provider, "open")), False))


def record_success(provider):
    now = timezone.now().isoformat()
    _safe(lambda: cache.set_many({_k(provider, "last_ok"): now, _k(provider, "fails"): 0}, None))
    _safe(lambda: cache.delete(_k(provider, "open")))


def record_failure(provider, reason=""):
    now = timezone.now().isoformat()
    _safe(lambda: cache.set_many({_k(provider, "last_fail"): now, _k(provider, "last_reason"): reason[:120]}, None))

    def bump():
        if cache.add(_k(provider, "fails"), 1, FAILURE_WINDOW):
            return 1
        try:
            return cache.incr(_k(provider, "fails"))
        except ValueError:
            cache.set(_k(provider, "fails"), 1, FAILURE_WINDOW)
            return 1

    n = _safe(bump, 0) or 0
    if n >= FAILURE_LIMIT:
        _safe(lambda: cache.set(_k(provider, "open"), now, OPEN_SECONDS))
        logger.warning("Circuit open for %s after %d failures", provider, n, extra={"provider": provider})


def health(provider) -> dict:
    vals = _safe(lambda: cache.get_many([_k(provider, x) for x in ("last_ok", "last_fail", "last_reason", "open")]), {}) or {}
    return {"last_success": vals.get(_k(provider, "last_ok")), "last_failure": vals.get(_k(provider, "last_fail")),
            "last_reason": vals.get(_k(provider, "last_reason"), ""), "circuit_open": bool(vals.get(_k(provider, "open")))}


def reset(provider):
    _safe(lambda: cache.delete_many([_k(provider, x) for x in ("last_ok", "last_fail", "last_reason", "open", "fails")]))


@contextmanager
def guarded(provider, operation):
    """Wrap an SDK call: fail fast when the circuit is open, record the outcome, log the duration."""
    if is_open(provider):
        raise ProviderUnavailable(provider, "circuit open")
    started = time.monotonic()
    try:
        yield
    except ProviderUnavailable:
        record_failure(provider, "unavailable")
        raise
    except Exception as exc:
        record_failure(provider, type(exc).__name__)
        logger.info("%s %s failed after %dms", provider, operation, (time.monotonic() - started) * 1000,
                    extra={"provider": provider, "operation": operation})
        raise
    record_success(provider)
    logger.info("%s %s ok %dms", provider, operation, (time.monotonic() - started) * 1000,
                extra={"provider": provider, "operation": operation, "duration_ms": int((time.monotonic() - started) * 1000)})


def multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----ndc" + "".join(random.choice("0123456789abcdef") for _ in range(24))
    out = []
    for k, v in fields.items():
        out += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{k}"'.encode(), b"", str(v).encode()]
    for k, (name, content, ctype) in files.items():
        out += [f"--{boundary}".encode(), f'Content-Disposition: form-data; name="{k}"; filename="{name}"'.encode(),
                f"Content-Type: {ctype}".encode(), b"", content]
    out += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(out), f"multipart/form-data; boundary={boundary}"


def call(provider, method, url, *, operation="", headers=None, json=None, data=None, files=None, read_timeout=None,
         idempotent=False, retries=None, timeout_key=None) -> HttpResult:
    """One HTTPS (or, for local fakes, HTTP) request. Raises ProviderUnavailable on network errors, timeouts,
    5xx after retries, or an open circuit. 4xx answers are returned to the caller (they are the caller's to interpret)."""
    if is_open(provider):
        raise ProviderUnavailable(provider, "circuit open")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ValueError("Only http(s) URLs")
    body = None
    hdrs = {"Accept": "application/json", "User-Agent": "NDCConnect/1.0", **(headers or {})}
    rid = current_request_id()
    if rid:
        hdrs.setdefault("X-Request-ID", rid)
    if files:
        body, ctype = multipart(data or {}, files)
        hdrs["Content-Type"] = ctype
    elif json is not None:
        body, hdrs["Content-Type"] = _json.dumps(json).encode(), "application/json"
    elif data is not None:
        body, hdrs["Content-Type"] = urllib.parse.urlencode(data).encode(), "application/x-www-form-urlencoded"
    read = read_timeout or READ_TIMEOUTS.get(timeout_key or provider, 10)
    attempts = 1 + ((2 if retries is None else retries) if idempotent else 0)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    op = operation or parts.path.split("/")[-1]
    last = "error"
    for attempt in range(attempts):
        if attempt:
            time.sleep(min(2 ** attempt * 0.25, 2) + random.random() * 0.2)
        started = time.monotonic()
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": CONNECT_TIMEOUT}
        if parts.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        conn = conn_cls(parts.hostname, parts.port, **kwargs)
        try:
            conn.connect()
            conn.sock.settimeout(read)
            conn.request(method, path, body=body, headers=hdrs)
            res = conn.getresponse()
            content = res.read()
            result = HttpResult(res.status, content, {k.lower(): v for k, v in res.getheaders()})
        except (OSError, http.client.HTTPException) as exc:
            last = type(exc).__name__
            logger.info("%s %s %s after %dms", provider, op, last, (time.monotonic() - started) * 1000,
                        extra={"provider": provider, "operation": op})
            continue
        finally:
            conn.close()
        ms = int((time.monotonic() - started) * 1000)
        logger.info("%s %s %s %dms", provider, op, result.status, ms, extra={"provider": provider, "operation": op, "duration_ms": ms})
        if result.status >= 500:
            last = f"HTTP {result.status}"
            continue
        record_success(provider)
        return result
    record_failure(provider, last)
    raise ProviderUnavailable(provider, last)
