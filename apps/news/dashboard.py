"""News figures for the admin home (spec 11 §6.1). Per-user (own submissions excluded), so not cached."""
from django.db.models import Min

from apps.access.models import Role
from apps.access.services import has_role, is_system_admin
from apps.dashboard.cards import Card, Stat, weeks

from .models import Article, ArticleStatus, Source

CACHEABLE = False


def admin_summary(user, *, now):
    out = []
    reviewer = has_role(user, Role.NEWS_REVIEWER)
    if reviewer:
        pending = Article.objects.filter(source=Source.COMMUNITY, status=ArticleStatus.PENDING).exclude(author=user)
        out.append(Card("news_pending", "Community news to review", pending.count(), "/admin/news",
                        oldest_at=pending.aggregate(t=Min("submitted_at"))["t"], target_hours=24, per_user=True))
    if reviewer or is_system_admin(user):
        this, last = weeks(now)
        for key, label, src in (("official", "Official announcements", Source.OFFICIAL), ("community", "Community news published", Source.COMMUNITY)):
            live = Article.objects.filter(source=src, status=ArticleStatus.SUCCESSFUL)
            out.append(Stat("content", key, label, live.count(), live.filter(published_at__gte=this).count(),
                            live.filter(published_at__gte=last, published_at__lt=this).count()))
    return out
