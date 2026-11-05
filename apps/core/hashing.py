"""Keyed hashes of IP addresses (spec 16 F6). One format (64 hex characters), keyed with IP_HASH_KEY so rotating
SECRET_KEY doesn't reset abuse correlation. Falls back to SECRET_KEY when IP_HASH_KEY isn't set."""
import hashlib
import hmac

from django.conf import settings


def ip_hash(ip: str) -> str:
    if not ip:
        return ""
    key = (getattr(settings, "IP_HASH_KEY", "") or settings.SECRET_KEY).encode()
    return hmac.new(key, ip.encode(), hashlib.sha256).hexdigest()
