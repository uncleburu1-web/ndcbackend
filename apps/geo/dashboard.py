"""Geography figures for the admin home (spec 19 §15.2). Counts only; never who or where."""
from datetime import timedelta

from django.db.models import Min

from apps.access.services import is_system_admin
from apps.dashboard.cards import Card

from . import records
from .models import Dataset

CACHEABLE = False


def admin_summary(user, *, now):
    out = []
    kinds = records.review_kinds(user)
    if kinds:
        out.append(Card("geo_review_waiting", "Records waiting for a location", records.review_count(kinds),
                        "/admin/geography/review", info_only=True, per_user=True))
    if is_system_admin(user):
        out.append(Card("geo_units_changed", "People whose polling unit changed", records.changed_unconfirmed(),
                        "/admin/geography/review?why=retired", info_only=True))
        stale = Dataset.objects.filter(status=Dataset.Status.PREVIEWED, loaded_at__lt=now - timedelta(days=7))
        out.append(Card("geo_preview_waiting", "Geography file previewed, not applied", stale.count(), "/admin/geography",
                        oldest_at=stale.aggregate(t=Min("loaded_at"))["t"], any_is_warning=True))
    return out
