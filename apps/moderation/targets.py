"""What a case is about (spec 13 §5.1). The moderation app reads targets; content changes go through the owning app."""
from apps.access.models import Role, ScopeType
from apps.access.models import RoleAssignment

from .models import TargetKind


def model_for(kind):
    if kind == TargetKind.ARTICLE:
        from apps.news.models import Article

        return Article
    if kind == TargetKind.POST:
        from apps.posts.models import CandidatePost

        return CandidatePost
    from apps.candidates.models import Candidate

    return Candidate


def load(kind, target_id):
    qs = model_for(kind).objects.all()
    if kind == TargetKind.POST:
        qs = qs.select_related("candidate")
    if kind == TargetKind.ARTICLE:
        qs = qs.select_related("author")
    return qs.filter(pk=target_id).first()


def is_public(kind, obj) -> bool:
    if obj is None:
        return False
    if kind == TargetKind.ARTICLE:
        return obj.status == "successful"
    if kind == TargetKind.POST:
        return obj.status == "published" and obj.candidate.status == "published" and obj.candidate.verified_at is not None
    return obj.status == "published" and obj.verified_at is not None


def title(kind, obj) -> str:
    if obj is None:
        return ""
    return (obj.full_name if kind == TargetKind.CANDIDATE else obj.title)[:200]


def state(kind, obj) -> str:
    if obj is None:
        return ""
    if kind == TargetKind.POST:
        return obj.candidate.state or ""
    return getattr(obj, "state", "") or ""


def is_official(kind, obj) -> bool:
    return kind == TargetKind.ARTICLE and obj is not None and obj.source == "official"


def _linked(candidate_id):
    return list(RoleAssignment.objects.filter(role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=candidate_id, is_active=True,
                                              user__is_active=True).values_list("user_id", flat=True))


def owner_ids(kind, obj) -> set:
    """Who can't moderate this item (spec 13 §3.3): its author, or accounts linked to its candidate."""
    if obj is None:
        return set()
    if kind == TargetKind.ARTICLE:
        return {obj.author_id} if obj.author_id and obj.source == "community" else set()
    if kind == TargetKind.POST:
        return set(_linked(obj.candidate_id))
    return set(_linked(obj.pk))


def authors(kind, obj):
    """Who is told about removals and can appeal: the community news author, or the candidate's linked accounts."""
    from apps.accounts.models import User

    ids = owner_ids(kind, obj) if kind != TargetKind.CANDIDATE else set()
    return list(User.objects.filter(pk__in=ids, is_active=True, clerk_deleted_at__isnull=True))


def public_url(kind, obj) -> str:
    if obj is None:
        return ""
    if kind == TargetKind.ARTICLE:
        return f"/news/{obj.source}/{obj.pk}"
    if kind == TargetKind.POST:
        return f"/candidates/{obj.candidate.slug}/posts/{obj.pk}"
    return f"/candidates/{obj.slug}"


def admin_url(kind, obj) -> str:
    if obj is None:
        return ""
    if kind == TargetKind.ARTICLE:
        return f"/admin/announcements/{obj.pk}" if obj.source == "official" else f"/admin/news/{obj.pk}"
    if kind == TargetKind.POST:
        return f"/admin/posts/{obj.pk}"
    return f"/admin/candidates/{obj.pk}"


def kind_words(kind, obj=None) -> str:
    if kind == TargetKind.ARTICLE:
        return "official announcement" if is_official(kind, obj) else "news story"
    return "post" if kind == TargetKind.POST else "candidate profile"


def parse_url(value: str):
    """An NDCConnect URL or bare UUID → (kind, obj) (spec 13 §4.7)."""
    import re
    import uuid as _uuid

    v = (value or "").strip()
    m = re.search(r"/news/(?:official|community)/([0-9a-f-]{36})", v)
    if m:
        return TargetKind.ARTICLE, load(TargetKind.ARTICLE, m.group(1))
    m = re.search(r"/candidates/[^/]+/posts/([0-9a-f-]{36})", v)
    if m:
        return TargetKind.POST, load(TargetKind.POST, m.group(1))
    m = re.search(r"/candidates/([a-z0-9-]+)/?$", v)
    if m:
        from apps.candidates.models import Candidate

        return TargetKind.CANDIDATE, Candidate.objects.filter(slug=m.group(1)).first()
    try:
        u = _uuid.UUID(v)
    except ValueError:
        return None, None
    for k in (TargetKind.ARTICLE, TargetKind.POST, TargetKind.CANDIDATE):
        obj = load(k, u)
        if obj is not None:
            return k, obj
    return None, None
