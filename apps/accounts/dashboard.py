"""Account figures for the admin home (spec 11 §6.1)."""
from apps.access.services import is_system_admin
from apps.dashboard.cards import Card, Stat, weeks

from .models import User
from .services import ban_failed_ids


def _settings_stats(users):
    """Spec 20 §14: counts only, never a person's choices. One query; people with no settings row use the defaults."""
    from django.db.models import Count, Q

    from .models import Language, TextSize, Theme

    no_row = Q(preferences__isnull=True)
    agg = {f"lang_{c}": Count("pk", filter=Q(preferences__language=c) | (no_row if c == "en" else Q(pk__in=[])))
           for c in Language.values}
    agg |= {f"theme_{c}": Count("pk", filter=Q(preferences__theme=c)) for c in Theme.values if c != "system"}
    agg |= {f"text_{c}": Count("pk", filter=Q(preferences__text_size=c)) for c in TextSize.values if c != "standard"}
    agg["public_profiles"] = Count("pk", filter=Q(preferences__public_profile=True, is_active=True))
    agg["paused"] = Count("pk", filter=Q(is_active=False, deactivation_kind="self"))
    n = users.aggregate(**agg)
    out = [Stat("settings", f"lang_{c}", f"Language: {label}", n[f"lang_{c}"]) for c, label in Language.choices]
    out += [Stat("settings", f"theme_{c}", f"Theme: {label}", n[f"theme_{c}"]) for c, label in Theme.choices if c != "system"]
    out += [Stat("settings", f"text_{c}", f"Text size: {label}", n[f"text_{c}"]) for c, label in TextSize.choices if c != "standard"]
    out += [Stat("settings", "public_profiles", "Public profiles on", n["public_profiles"]),
            Stat("people", "paused", "Paused by the person", n["paused"])]
    return out


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
        *_settings_stats(users),
        Stat("people", "profiles", "Profiles completed", users.filter(profile_completed_at__isnull=False).count(),
             users.filter(profile_completed_at__gte=this).count(), users.filter(profile_completed_at__gte=last, profile_completed_at__lt=this).count()),
    ]
