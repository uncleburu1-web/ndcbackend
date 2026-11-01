"""
Campaign blackout windows — spec 07 §6.6 (reused by 09). Electoral Act 2026: public campaigning
must end 24 hours before polling day. Windows are scoped by office because polling days differ.
"""
from datetime import datetime

from django.conf import settings
from django.utils import timezone


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else timezone.make_aware(dt)


def windows() -> list[dict]:
    out = []
    for w in getattr(settings, "CAMPAIGN_BLACKOUTS", []) or []:
        out.append({"positions": list(w.get("positions", [])), "start": _parse(w["start"]), "end": _parse(w["end"])})
    return out


def active_window(at=None, positions=None) -> dict | None:
    """The window in force at `at` (default now). With `positions`, only windows covering one of them."""
    at = at or timezone.now()
    live = [w for w in windows() if w["start"] <= at <= w["end"]]
    if positions is not None:
        wanted = set(positions)
        live = [w for w in live if wanted & set(w["positions"])]
    return max(live, key=lambda w: w["end"]) if live else None
