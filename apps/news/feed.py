"""
The public /news feed — spec 07 §4.1, with candidate posts added by spec 09 §6.4.
Merged paging works on keys: a UNION ALL of (id, published_at, type) from public articles and visible candidate
posts, ordered by published_at DESC, id; each page's keys are then loaded from both tables.
"""
from django.conf import settings
from django.db.models import CharField, Q, Value

from .models import Article, Source

CANDIDATE = "candidate"
SOURCES = (Source.OFFICIAL, Source.COMMUNITY, CANDIDATE)


def _articles(source: str, state: str):
    qs = Article.objects.public()
    if source in (Source.OFFICIAL, Source.COMMUNITY):
        qs = qs.filter(source=source)
    if state:
        qs = qs.filter(Q(state=state) | Q(state=""))  # national items show under every state
    return qs


def _posts(state: str):
    from apps.posts.models import CandidatePost

    qs = CandidatePost.objects.visible()
    if state:
        qs = qs.filter(Q(candidate__state=state) | Q(candidate__state=""))  # e.g. President: every state
    return qs


def pinned_items(source: str = "", state: str = ""):
    if source not in ("", Source.OFFICIAL):
        return []
    qs = _articles(Source.OFFICIAL, state).filter(pinned_at__isnull=False).select_related("author", "audience_community")
    return list(qs.order_by("-pinned_at")[: settings.NEWS_MAX_PINNED])


def feed_page(source: str, state: str, page: int, size: int = 20, language: str = ""):
    """Returns (pinned, items, count). Items are Article or CandidatePost instances in feed order.
    `language` (spec 21 §9) keeps only items written in that language; pinned items always show."""
    from apps.posts.models import CandidatePost

    source = source if source in SOURCES else ""
    pinned = pinned_items(source, state)
    pinned_ids = [p.pk for p in pinned]
    keys_a = keys_p = None
    if source != CANDIDATE:
        arts = _articles(source, state)
        if language:
            arts = arts.filter(language=language)
        keys_a = (arts.exclude(pk__in=pinned_ids)
                  .annotate(t=Value("a", output_field=CharField())).values_list("id", "published_at", "t"))
    if source in ("", CANDIDATE):
        posts_qs = _posts(state).filter(language=language) if language else _posts(state)
        keys_p = posts_qs.annotate(t=Value("p", output_field=CharField())).values_list("id", "published_at", "t")
    if keys_a is not None and keys_p is not None:
        keys = keys_a.order_by().union(keys_p.order_by(), all=True)
        count = keys_a.count() + keys_p.count()
    else:
        keys = keys_a if keys_a is not None else keys_p
        count = keys.count()
    start = (max(page, 1) - 1) * size
    rows = list(keys.order_by("-published_at", "-id")[start:start + size])
    a_ids = [r[0] for r in rows if r[2] == "a"]
    p_ids = [r[0] for r in rows if r[2] == "p"]
    articles = {a.pk: a for a in Article.objects.filter(pk__in=a_ids).select_related("author", "audience_community")}
    posts = {p.pk: p for p in CandidatePost.objects.filter(pk__in=p_ids).select_related("candidate")}
    items = [articles[r[0]] if r[2] == "a" else posts[r[0]] for r in rows]
    return pinned, items, count


def feed(source: str = "", state: str = ""):
    """Articles-only helper kept for callers that page a queryset themselves (spec 07)."""
    pinned = pinned_items(source, state)
    qs = _articles(source, state).select_related("author", "audience_community").exclude(pk__in=[p.pk for p in pinned])
    return pinned, qs.order_by("-published_at", "-id")
