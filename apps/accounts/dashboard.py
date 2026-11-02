"""Account figures for the admin home (spec 11 §6.1)."""
from apps.access.services import is_system_admin
from apps.dashboard.cards import Card, Stat, weeks

from .models import User
from .services import ban_failed_ids


def admin_summary(user, *, now):
    if not is_system_admin(user):
        return []
    this, last = weeks(now)
    users = User.objects.filter(clerk_deleted_at__isnull=True)
    return [
        Card("ban_failed", "Sign-in blocks to retry", len(ban_failed_ids()), "/admin/users?filter=ban_failed", any_is_warning=True),
        Stat("people", "accounts", "Accounts", users.count(), users.filter(created_at__gte=this).count(),
             users.filter(created_at__gte=last, created_at__lt=this).count()),
        Stat("people", "active", "Active accounts", users.filter(is_active=True).count()),
        Stat("people", "profiles", "Profiles completed", users.filter(profile_completed_at__isnull=False).count(),
             users.filter(profile_completed_at__gte=this).count(), users.filter(profile_completed_at__gte=last, profile_completed_at__lt=this).count()),
    ]
