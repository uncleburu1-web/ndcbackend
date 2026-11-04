"""Settings for generating docs/openapi.json (spec 15 §14.2): production-shaped (DEBUG off, real rate limits) without
production secrets. `DJANGO_SETTINGS_MODULE=config.settings.schema python manage.py spectacular …`"""
from .base import *  # noqa: F401,F403

DEBUG = False
TESTING = True
