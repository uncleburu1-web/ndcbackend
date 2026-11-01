"""
Field encryption for government identifiers (VIN, NIN) — spec 06 §9.

Fernet (AES-128-CBC + HMAC-SHA256) via MultiFernet, so keys can rotate: the first key
encrypts, all keys decrypt. Keys come from PII_ENCRYPTION_KEYS in the backend .env only.
Production refuses to start without them; development generates a local key file once.
"""
import logging
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


def _dev_key() -> str:
    path = Path(settings.BASE_DIR) / ".dev_pii_key"
    if not path.exists():
        path.write_text(Fernet.generate_key().decode())
        logger.warning("PII_ENCRYPTION_KEYS not set: generated a development key in %s (never use in production).", path.name)
    return path.read_text().strip()


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    keys = [k.strip() for k in settings.PII_ENCRYPTION_KEYS if k.strip()]
    if not keys:
        if settings.DEBUG or getattr(settings, "TESTING", False):
            keys = [_dev_key()]
        else:
            raise ImproperlyConfigured("Set PII_ENCRYPTION_KEYS (Fernet keys, comma-separated) in production.")
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured("PII_ENCRYPTION_KEYS contains an invalid Fernet key.") from exc


def encrypt(value: str) -> bytes:
    return _fernet().encrypt(value.encode())


def decrypt(token: bytes | memoryview | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(bytes(token)).decode()
    except InvalidToken:
        logger.error("Could not decrypt a protected field (wrong or rotated-out key).")
        return ""


def reset_cache():  # tests / key rotation
    _fernet.cache_clear()
