"""
Indexing (spec 12 §6.1). An item is in the index only while a guest could open it (rule 3.1).
Every save of a candidate, article or post re-indexes it in the same transaction (signals.py);
`rebuild()` repairs any drift. Later units add a line to SEARCHABLE, not a new engine.
"""
from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import Value

from .aliases import office_words, place_words
from .models import Kind, SearchEntry
from .text import fold, title_key

BODY_MAX = 20_000


def _public_candidates():
    from apps.candidates.models import Candidate, CandidateStatus

    return Candidate.objects.filter(status=CandidateStatus.PUBLISHED, verified_at__isnull=False)


def _public_articles():
    from apps.news.models import Article, ArticleStatus

    return Article.objects.filter(status=ArticleStatus.SUCCESSFUL)


def _public_posts():
    from apps.posts.models import CandidatePost

    return CandidatePost.objects.visible().select_related("candidate")


def _public_events():
    from django.utils import timezone

    from apps.events.models import EventStatus
    from apps.events.services import public_events

    return public_events().filter(status=EventStatus.PUBLISHED, ends_at__gt=timezone.now())


def _candidate_entry(c):
    from apps.candidates.reference import POSITION_ORDER

    priorities = " ".join(map(str, c.priorities or []))
    return {
        "title": c.full_name, "body_text": c.campaign_summary or "", "state": c.state or "", "position": c.position,
        "election_year": c.election_year, "source": "", "published_at": c.published_at, "rank_order": POSITION_ORDER.index(c.position) + 1,
        "_weights": {"A": c.full_name, "B": f"{c.area_name} {c.ward_name}",
                     "C": f"{office_words(c.position)} {place_words(c.state)}", "D": f"{c.campaign_summary} {priorities}"},
    }


def _article_entry(a):
    return {
        "title": a.title, "body_text": a.body[:BODY_MAX], "state": a.state or "", "position": "", "election_year": None,
        "source": a.source, "published_at": a.published_at, "rank_order": 0,
        "_weights": {"A": a.title, "B": a.body[:BODY_MAX], "C": place_words(a.state), "D": a.source_name or ""},
    }


def _post_entry(p):
    c = p.candidate
    return {
        "title": p.title, "body_text": p.body[:BODY_MAX], "state": c.state or "", "position": "", "election_year": None,
        "source": "", "published_at": p.published_at, "rank_order": 0,
        "_weights": {"A": p.title, "B": p.body[:BODY_MAX], "C": c.full_name, "D": place_words(c.state)},
    }


def _event_entry(e):
    organiser = e.organiser_label
    return {
        "title": e.title, "body_text": e.description[:BODY_MAX], "state": e.state or "", "position": "", "election_year": None,
        "source": "", "published_at": e.published_at, "rank_order": 0,
        "_weights": {"A": e.title, "B": f"{organiser} {e.get_type_display()}", "C": f"{place_words(e.state)} {e.lga_text}",
                     "D": e.description[:BODY_MAX]},
    }


# kind → (public queryset factory, entry builder)
SEARCHABLE = {
    Kind.CANDIDATE: (_public_candidates, _candidate_entry),
    Kind.ARTICLE: (_public_articles, _article_entry),
    Kind.POST: (_public_posts, _post_entry),
    Kind.EVENT: (_public_events, _event_entry),
}


def public_ids(kind):
    """Query-time re-check (spec 12 §9): the IDs a guest could open right now."""
    return SEARCHABLE[kind][0]().values("pk")


def _vector(weights: dict):
    vec = None
    for w, text in weights.items():
        part = SearchVector(Value(fold(text)), weight=w, config="simple")
        vec = part if vec is None else vec + part
    return vec


def _upsert(kind, obj):
    data = SEARCHABLE[kind][1](obj)
    weights = data.pop("_weights")
    data["title"] = data["title"][:200]
    data["folded_title"] = fold(data["title"])[:200]
    data["title_key"] = title_key(data["folded_title"])[:200] if kind == Kind.CANDIDATE else ""
    entry, _ = SearchEntry.objects.update_or_create(kind=kind, object_id=obj.pk, defaults=data)
    SearchEntry.objects.filter(pk=entry.pk).update(document=_vector(weights))
    return entry


def remove(kind, object_id):
    SearchEntry.objects.filter(kind=kind, object_id=object_id).delete()


def _sync(kind, obj):
    if SEARCHABLE[kind][0]().filter(pk=obj.pk).exists():
        return _upsert(kind, SEARCHABLE[kind][0]().get(pk=obj.pk))
    remove(kind, obj.pk)
    return None


def index_candidate(c):
    """A candidate's posts are only visible while the candidate is, so they follow it (spec 12 §6.6)."""
    _sync(Kind.CANDIDATE, c)
    for p in c.posts.all():
        _sync(Kind.POST, p)
    for e in c.events.all():  # spec 18: campaign events follow their candidate too
        _sync(Kind.EVENT, e)


def index_article(a):
    _sync(Kind.ARTICLE, a)


def index_post(p):
    _sync(Kind.POST, p)


def sync_event(e):
    _sync(Kind.EVENT, e)


@transaction.atomic
def rebuild(kind=None, batch=500) -> dict:
    out = {}
    for k in ([kind] if kind else list(SEARCHABLE)):
        qs = SEARCHABLE[k][0]().order_by("pk")
        seen, n = [], 0
        for obj in qs.iterator(chunk_size=batch):
            _upsert(k, obj)
            seen.append(obj.pk)
            n += 1
        orphans, _ = SearchEntry.objects.filter(kind=k).exclude(object_id__in=seen).delete()
        out[k] = {"indexed": n, "orphans_removed": orphans}
    return out


def health() -> dict:
    """Index entries vs public items, for the dashboard's readiness line (spec 12 §6.6)."""
    entries = SearchEntry.objects.count()
    public = sum(SEARCHABLE[k][0]().count() for k in SEARCHABLE)
    ok = abs(entries - public) <= public // 100  # within 1% (exact for small sites)
    return {"entries": entries, "public": public, "ok": ok}
