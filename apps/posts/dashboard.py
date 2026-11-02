"""Candidate post figures for the admin home (spec 11 §6.1)."""
from apps.access.models import Role
from apps.access.services import has_role, is_system_admin
from apps.dashboard.cards import Stat, weeks

from .models import CandidatePost, PostStatus


def admin_summary(user, *, now):
    out = []
    admin = is_system_admin(user)
    if admin or has_role(user, Role.NEWS_REVIEWER):
        this, last = weeks(now)
        live = CandidatePost.objects.filter(status=PostStatus.PUBLISHED)
        out.append(Stat("content", "candidate_posts", "Candidate posts published", live.count(), live.filter(published_at__gte=this).count(),
                        live.filter(published_at__gte=last, published_at__lt=this).count()))
    return out
