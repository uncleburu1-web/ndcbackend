"""Helpers for per-app exporters (`apps/<app>/privacy.py: export_for(user) -> dict`, spec 14 §6.2)."""
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


def value(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (UUID, Decimal)):
        return str(v)
    if isinstance(v, bytes):
        return None  # never export encrypted blobs
    if hasattr(v, "name") and hasattr(v, "url"):  # FieldFile
        return v.name or None
    return v


def pick(obj, fields) -> dict:
    """Only the listed fields. Exporters never list fields naming other people (deciders, reviewers, officers)."""
    out = {}
    for f in fields:
        if isinstance(f, tuple):
            key, getter = f
            out[key] = value(getter(obj))
        else:
            out[f] = value(getattr(obj, f))
    return out
