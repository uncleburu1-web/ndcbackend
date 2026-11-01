"""Moved to apps.payments.providers (spec 06 §6.3). Kept so existing imports keep working."""
from apps.payments.providers import *  # noqa: F401,F403
from apps.payments.providers import get_provider  # noqa: F401
