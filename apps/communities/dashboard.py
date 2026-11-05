"""Community figures for the admin home (spec 17 §15). Per-user (what the viewer can act on), so not cached."""
from datetime import timedelta

from django.db.models import Count, Min

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import is_system_admin
from apps.dashboard.cards import Card

from .models import Community, CommunityComment, CommunityPost, ItemStatus, Level

CACHEABLE = False


def admin_summary(user, *, now):

    from . import services

    out = []
    sysadmin = is_system_admin(user)
    scopes = None if sysadmin else services.moderator_scopes(user)
    if scopes is None or scopes:  # system admins, global and community moderators
        cutoff = now - timedelta(hours=24)
        posts = CommunityPost.objects.filter(status=ItemStatus.HIDDEN, hidden_at__lt=cutoff)
        comments = CommunityComment.objects.filter(status=ItemStatus.HIDDEN, hidden_at__lt=cutoff)
        if scopes is not None:
            paths = services.scope_paths(user)
            posts = posts.filter(services.under_paths(paths, "community__path"))
            comments = comments.filter(services.under_paths(paths, "post__community__path"))
        a = posts.aggregate(n=Count("pk"), t=Min("hidden_at"))
        b = comments.aggregate(n=Count("pk"), t=Min("hidden_at"))
        oldest = min([t for t in (a["t"], b["t"]) if t], default=None)
        out.append(Card("community_hidden_waiting", "Hidden items waiting", a["n"] + b["n"],
                        "/admin/moderation?type=communities", oldest_at=oldest, target_hours=24, per_user=True))
    if sysadmin:
        has_global = RoleAssignment.objects.filter(role=Role.MODERATOR, scope_type=ScopeType.GLOBAL, is_active=True,
                                                   user__is_active=True).exists()
        if has_global:
            n = 0
        else:
            covered = set(RoleAssignment.objects.filter(role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, is_active=True,
                                                        user__is_active=True).values_list("scope_id", flat=True))
            n = Community.objects.filter(level=Level.STATE, active=True).exclude(pk__in=covered).exclude(
                parent_id__in=covered).count()
        out.append(Card("communities_unmoderated", "Communities without a moderator", n, "/admin/communities", any_is_warning=True))
    return out
