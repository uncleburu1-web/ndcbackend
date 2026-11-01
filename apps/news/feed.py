"""The public /news feed — spec 07 §4.1. Kept separate so 08 can add candidate posts as another source."""
from django.conf import settings
from django.db.models import Q

from .models import Article, Source


def feed(source: str = "", state: str = ""):
    """Returns (pinned, queryset). Pinned official items lead All/Official and are not repeated below."""
    qs = Article.objects.public().select_related("author")
    if source in (Source.OFFICIAL, Source.COMMUNITY):
        qs = qs.filter(source=source)
    if state:
        qs = qs.filter(Q(state=state) | Q(state=""))  # national items show under every state
    pinned = []
    if source in ("", Source.OFFICIAL):
        pinned = list(qs.filter(source=Source.OFFICIAL, pinned_at__isnull=False).order_by("-pinned_at")[: settings.NEWS_MAX_PINNED])
        qs = qs.exclude(pk__in=[p.pk for p in pinned])
    return pinned, qs.order_by("-published_at", "-id")
