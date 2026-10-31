import re

from django.core.exceptions import ValidationError

_NG_MOBILE = re.compile(r"^[789][01]\d{8}$")  # 70x, 80x, 81x, 90x, 91x …


def normalize_ng_phone(value: str) -> str:
    """
    Accept 08031234567, 2348031234567, +234 803 123 4567, 0803-123-4567 and
    return E.164 (+2348031234567). Raises ValidationError otherwise (spec §6.3).
    """
    digits = re.sub(r"[\s\-().]", "", value or "")
    if digits.startswith("+234"):
        local = digits[4:]
    elif digits.startswith("234"):
        local = digits[3:]
    elif digits.startswith("0"):
        local = digits[1:]
    else:
        local = digits
    if not _NG_MOBILE.match(local):
        raise ValidationError("Enter a Nigerian mobile number, like 0803 123 4567.", code="invalid_phone")
    return f"+234{local}"


def validate_full_name(value: str) -> str:
    """At least two words, max 150 characters (spec §6.3)."""
    cleaned = " ".join((value or "").split())
    if len(cleaned) > 150:
        raise ValidationError("Keep your name under 150 characters.", code="too_long")
    if len(cleaned.split(" ")) < 2:
        raise ValidationError("Enter your first and last name.", code="invalid_name")
    return cleaned
