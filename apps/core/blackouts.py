"""
Campaign blackout windows — spec 07 §6.6 (reused by 09). Electoral Act 2026: public campaigning
must end 24 hours before polling day. Windows are scoped by office because polling days differ.

Campaign periods — spec 18 §4.4 (Electoral Act 2026 s. 98: campaigning starts 150 days before polling day). Offices with no
period configured aren't checked.
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


def periods() -> list[dict]:
    out = []
    for p in getattr(settings, "CAMPAIGN_PERIODS", []) or []:
        out.append({"positions": list(p.get("positions", [])), "start": _parse(p["start"]), "end": _parse(p["end"])})
    return out


def campaign_period(position) -> dict | None:
    """The configured campaign period for an office (the latest if several), or None when it isn't set."""
    mine = [p for p in periods() if position in p["positions"]]
    return max(mine, key=lambda p: p["end"]) if mine else None


def unset_period_offices() -> list[str]:
    from apps.candidates.reference import Position

    covered = {pos for p in periods() for pos in p["positions"]}
    return [v for v in Position.values if v not in covered]
