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


def _language_stats(now):
    """Spec 21 §14: what people write in each language this month, and reports waiting in each language."""
    from django.db.models import Count

    from apps.communities.models import CommunityPost
    from apps.core.i18n import ENGLISH_NAME, LANGUAGES
    from apps.moderation.models import Case, CaseStatus
    from apps.news.models import Article
    from apps.posts.models import CandidatePost

    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    written = {c: 0 for c in LANGUAGES}
    for model, field in ((Article, "created_at"), (CommunityPost, "created_at"), (CandidatePost, "created_at")):
        for row in model.objects.filter(**{f"{field}__gte": month}).values("language").annotate(n=Count("pk")):
            if row["language"] in written:
                written[row["language"]] += row["n"]
    waiting = dict(Case.objects.exclude(status=CaseStatus.CLOSED).values_list("language").annotate(n=Count("pk")))
    out = [Stat("languages", f"written_{c}", f"Written in {ENGLISH_NAME[c]} this month", written[c]) for c in LANGUAGES]
    out += [Stat("languages", f"waiting_{c}", f"Reports waiting in {ENGLISH_NAME[c]}", waiting.get(c, 0)) for c in LANGUAGES if c != "en"]
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
        *_language_stats(now),
        Stat("people", "profiles", "Profiles completed", users.filter(profile_completed_at__isnull=False).count(),
             users.filter(profile_completed_at__gte=this).count(), users.filter(profile_completed_at__gte=last, profile_completed_at__lt=this).count()),
    ]
