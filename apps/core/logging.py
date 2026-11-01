"""Logging filter: never let VIN/NIN values reach logs (spec 06 §9)."""
import logging
import re

SENSITIVE_KEYS = {"vin", "nin", "vin_encrypted", "nin_encrypted"}
_INLINE = re.compile(r"(?i)\b(vin|nin)(['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9]{6,})")


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if str(k).lower() in SENSITIVE_KEYS else _scrub(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v) for v in value)
    if isinstance(value, str):
        return _INLINE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", value)
    return value


class RedactPII(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = _scrub(record.msg)
        if record.args:
            record.args = _scrub(record.args) if isinstance(record.args, (dict, tuple)) else record.args
        return True
