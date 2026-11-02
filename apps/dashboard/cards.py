"""
The summary contract each app implements in its own `dashboard.py` (spec 11 §6.1):

    def admin_summary(user, *, now) -> list[Card | Stat]

Return [] when the user's roles don't allow the data. Never put names, emails or per-person
amounts in a Card or Stat (spec 11 §3.2).
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone


@dataclass
class Card:
    key: str
    label: str
    count: int
    href: str
    oldest_at: datetime | None = None
    target_hours: int | None = None
    target_working_days: int | None = None
    any_is_warning: bool = False
    info_only: bool = False
    per_user: bool = False  # depends on who is asking (e.g. "not your own"); never cached across users
    severity: str = field(default="ok")

    def finish(self, now) -> "Card":
        self.severity = severity(self, now)
        return self


@dataclass
class Stat:
    group: str
    key: str
    label: str
    total: int
    this_week: int | None = None
    last_week: int | None = None
    kobo: bool = False


def to_dict(x) -> dict:
    d = asdict(x)
    d["type"] = "card" if isinstance(x, Card) else "stat"
    return d


def weeks(now):
    """(start of this 7-day window, start of the previous one) — rolling, in absolute time (spec 11 §4.2)."""
    return now - timedelta(days=7), now - timedelta(days=14)


def working_days_between(start, end) -> int:
    """Whole Monday–Friday days elapsed between two moments, in Lagos time (as 08's queue banner)."""
    tz = timezone.get_current_timezone()
    d, last = timezone.localtime(start, tz).date(), timezone.localtime(end, tz).date()
    n = 0
    while d < last:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def severity(card: Card, now) -> str:
    if card.info_only:
        return "info"
    if card.count == 0:
        return "ok"
    if card.any_is_warning:
        return "warning"
    if card.oldest_at is None:
        return "ok"
    if card.target_hours is not None:
        age_h = (now - card.oldest_at).total_seconds() / 3600
        return "error" if age_h > 2 * card.target_hours else "warning" if age_h > card.target_hours else "ok"
    if card.target_working_days is not None:
        days = working_days_between(card.oldest_at, now)
        return "error" if days > 2 * card.target_working_days else "warning" if days > card.target_working_days else "ok"
    return "ok"
