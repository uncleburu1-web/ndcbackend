from .base import *  # noqa: F401,F403

DEBUG = True

# The fake payment provider keeps its "bank" state here so the dev server and
# management commands (sweep_donations) see the same thing (spec 05 §6.4).
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "fake_payments": {"BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                      "LOCATION": str(BASE_DIR / ".fake_payments")},  # noqa: F405
}
