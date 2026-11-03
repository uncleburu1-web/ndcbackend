"""Logging filters: never let VIN/NIN values (spec 06 §9), emails, phone numbers, tokens or keys (spec 14 §4.9) reach logs."""
import logging
import re

SENSITIVE_KEYS = {"vin", "nin", "vin_encrypted", "nin_encrypted"}
_INLINE = re.compile(r"(?i)\b(vin|nin)(['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9]{6,})")
# spec 14 §4.9
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE = re.compile(r"(?<![\w])(?:\+?234|0)[789][01]\d{8}(?!\d)")
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_AUTH = re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^'\",\s}]+")
_KEYS = re.compile(r"\b(sk|pk|whsec|rk)_(?:live|test)_[A-Za-z0-9]{6,}|\bsk_[A-Za-z0-9]{20,}")


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if str(k).lower() in SENSITIVE_KEYS else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v) for v in value)
    if isinstance(value, str):
        value = _INLINE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", value)
        value = _KEYS.sub("[key]", value)
        value = _BEARER.sub(lambda m: f"{m.group(1)}[token]", value)
        value = _AUTH.sub(lambda m: f"{m.group(1)}[redacted]", value)
        value = _EMAIL.sub("[email]", value)
        return _PHONE.sub("[phone]", value)
    return value


class RedactPII(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if record.args:
            record.args = _scrub(record.args) if isinstance(record.args, (dict, tuple)) else record.args
        return True


_QUERY = re.compile(r"([?&](?:q|query)=)[^&\s\"]*")


class RedactSearchQuery(logging.Filter):
    """Spec 12 §9: search text never reaches logs. `/search/?q=osun` is logged as `q=[redacted]`."""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        if "q=" in message or "query=" in message:
            record.msg, record.args = _QUERY.sub(r"\1[redacted]", message), ()
        return True
