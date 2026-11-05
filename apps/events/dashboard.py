"""Event figures for the admin home (spec 18 §15.2). Counts only; never who registered."""
from datetime import timedelta

from django.db.models import Count, Min

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import is_system_admin
from apps.dashboard.cards import Card

from .models import Event, EventStatus

CACHEABLE = False


def _global_moderator(user) -> bool:
    return RoleAssignment.objects.filter(user=user, role=Role.MODERATOR, scope_type=ScopeType.GLOBAL, is_active=True).exists()


def admin_summary(user, *, now):
    from apps.moderation.models import Case, CaseStatus

    out = []
    sysadmin = is_system_admin(user)
    if sysadmin or _global_moderator(user):
        c = Case.objects.filter(target_kind="event").exclude(status=CaseStatus.CLOSED).aggregate(n=Count("pk"), t=Min("opened_at"))
        out.append(Card("events_reported", "Events reported", c["n"], "/admin/moderation?type=events", oldest_at=c["t"], target_hours=24))
        h = Event.objects.filter(status=EventStatus.HIDDEN, hidden_at__lt=now - timedelta(hours=24)).aggregate(n=Count("pk"), t=Min("hidden_at"))
        out.append(Card("events_hidden_waiting", "Hidden events waiting", h["n"], "/admin/moderation?type=events", oldest_at=h["t"],
                        target_hours=24))
    if sysadmin:
        flagged = Event.objects.filter(status=EventStatus.PUBLISHED, flagged_blackout_at__isnull=False, starts_at__gt=now)
        out.append(Card("events_in_blackout", "Campaign events inside a blackout window", flagged.count(), "/admin/events?flagged=1",
                        any_is_warning=True))
    return out
