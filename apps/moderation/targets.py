"""What a case is about (spec 13 §5.1). The moderation app reads targets; content changes go through the owning app."""
from apps.access.models import Role, ScopeType
from apps.access.models import RoleAssignment

from .models import TargetKind


COMMUNITY_KINDS = (TargetKind.COMMUNITY_POST, TargetKind.COMMUNITY_COMMENT)
HIDEABLE_KINDS = COMMUNITY_KINDS + (TargetKind.EVENT,)  # spec 18 §8: hide while deciding; the owning app changes the item


def service(kind):
    """The owning app's hide/unhide/remove/restore functions for a hideable kind."""
    if kind == TargetKind.EVENT:
        from apps.events import services

        return services
    from apps.communities import services

    return services


def model_for(kind):
    if kind == TargetKind.PROFILE:  # spec 20 §5.1: the person's settings row (its pk is the user's id)
        from apps.accounts.models import UserPreferences

        return UserPreferences
    if kind == TargetKind.EVENT:
        from apps.events.models import Event

        return Event
    if kind == TargetKind.COMMUNITY_POST:
        from apps.communities.models import CommunityPost

        return CommunityPost
    if kind == TargetKind.COMMUNITY_COMMENT:
        from apps.communities.models import CommunityComment

        return CommunityComment
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
    if kind == TargetKind.COMMUNITY_POST:
        qs = qs.select_related("author", "community", "campaign_candidate")
    if kind == TargetKind.COMMUNITY_COMMENT:
        qs = qs.select_related("author", "post__community", "campaign_candidate")
    if kind == TargetKind.EVENT:
        qs = qs.select_related("candidate", "created_by")
    if kind == TargetKind.PROFILE:
        qs = qs.select_related("user")
    return qs.filter(pk=target_id).first()


def community_of(kind, obj):
    if obj is None:
        return None
    return obj.community if kind == TargetKind.COMMUNITY_POST else obj.post.community


def is_public(kind, obj) -> bool:
    """For community items: visible to the community's readers (they are never public to guests, spec 17 §6)."""
    if obj is None:
        return False
    if kind in COMMUNITY_KINDS:
        return obj.status == "published" and community_of(kind, obj).active
    if kind == TargetKind.PROFILE:
        return bool(obj.public_profile and obj.profile_handle and obj.user.is_active and not obj.user.clerk_deleted_at)
    if kind == TargetKind.EVENT:
        from apps.events.services import is_public as event_public

        return event_public(obj)
    if kind == TargetKind.ARTICLE:
        return obj.status == "successful"
    if kind == TargetKind.POST:
        return obj.status == "published" and obj.candidate.status == "published" and obj.candidate.verified_at is not None
    return obj.status == "published" and obj.verified_at is not None


def title(kind, obj) -> str:
    if obj is None:
        return ""
    if kind in COMMUNITY_KINDS:
        return " ".join((obj.body or "").split())[:80]
    if kind == TargetKind.PROFILE:
        from apps.accounts.naming import format_name

        return f"Public profile · {format_name(obj.user.full_name, obj.name_style) or 'A member'}"[:200]
    return (obj.full_name if kind == TargetKind.CANDIDATE else obj.title)[:200]


def state(kind, obj) -> str:
    if obj is None:
        return ""
    if kind == TargetKind.POST:
        return obj.candidate.state or ""
    if kind == TargetKind.EVENT:
        return obj.state or ""
    if kind in COMMUNITY_KINDS:
        return community_of(kind, obj).state or ""
    if kind == TargetKind.PROFILE:
        return obj.user.home_state or ""
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
    if kind == TargetKind.EVENT:  # spec 18 §8: linked accounts of its candidate, or the admin who created a party event
        from apps.events.services import organiser_ids

        return organiser_ids(obj)
    if kind in COMMUNITY_KINDS:  # the author, and accounts linked to the campaign it was posted for (spec 17 §8.2)
        return ({obj.author_id} if obj.author_id else set()) | (set(_linked(obj.campaign_candidate_id)) if obj.campaign_candidate_id else set())
    if kind == TargetKind.PROFILE:
        return {obj.user_id}
    return set(_linked(obj.pk))


def authors(kind, obj):
    """Who is told about removals and can appeal: the community news author, or the candidate's linked accounts."""
    from apps.accounts.models import User

    ids = owner_ids(kind, obj) if kind != TargetKind.CANDIDATE else set()
    if kind in COMMUNITY_KINDS:
        ids = {obj.author_id} if obj is not None and obj.author_id else set()
    return list(User.objects.filter(pk__in=ids, is_active=True, clerk_deleted_at__isnull=True))


def public_url(kind, obj) -> str:
    if obj is None:
        return ""
    if kind == TargetKind.ARTICLE:
        return f"/news/{obj.source}/{obj.pk}"
    if kind == TargetKind.POST:
        return f"/candidates/{obj.candidate.slug}/posts/{obj.pk}"
    if kind == TargetKind.COMMUNITY_POST:
        return f"/communities/{obj.community.path}/posts/{obj.pk}"
    if kind == TargetKind.COMMUNITY_COMMENT:
        return f"/communities/{obj.post.community.path}/posts/{obj.post_id}#comment-{obj.pk}"
    if kind == TargetKind.EVENT:
        return f"/events/{obj.pk}"
    if kind == TargetKind.PROFILE:
        return f"/people/{obj.profile_handle}" if obj.profile_handle else ""
    return f"/candidates/{obj.slug}"


def admin_url(kind, obj) -> str:
    if obj is None:
        return ""
    if kind == TargetKind.ARTICLE:
        return f"/admin/announcements/{obj.pk}" if obj.source == "official" else f"/admin/news/{obj.pk}"
    if kind == TargetKind.POST:
        return f"/admin/posts/{obj.pk}"
    if kind in COMMUNITY_KINDS:
        return public_url(kind, obj)
    if kind == TargetKind.EVENT:
        return f"/admin/events/party/{obj.pk}" if obj.organiser_kind == "party" else f"/events/{obj.pk}"
    if kind == TargetKind.PROFILE:
        return public_url(kind, obj)
    return f"/admin/candidates/{obj.pk}"


def kind_words(kind, obj=None) -> str:
    if kind == TargetKind.ARTICLE:
        return "official announcement" if is_official(kind, obj) else "news story"
    if kind == TargetKind.COMMUNITY_POST:
        return "community post"
    if kind == TargetKind.COMMUNITY_COMMENT:
        return "community comment"
    if kind == TargetKind.EVENT:
        return "event"
    if kind == TargetKind.PROFILE:
        return "public profile"
    return "post" if kind == TargetKind.POST else "candidate profile"


def parse_url(value: str):
    """An NDCConnect URL or bare UUID → (kind, obj) (spec 13 §4.7)."""
    import re
    import uuid as _uuid

    v = (value or "").strip()
    m = re.search(r"/news/(?:official|community)/([0-9a-f-]{36})", v)
    if m:
        return TargetKind.ARTICLE, load(TargetKind.ARTICLE, m.group(1))
    m = re.search(r"/people/([a-z0-9]{6})/?$", v)
    if m:
        from apps.accounts.models import UserPreferences

        p = UserPreferences.objects.select_related("user").filter(profile_handle=m.group(1)).first()
        return (TargetKind.PROFILE, p) if p else (None, None)
    m = re.search(r"/events/([0-9a-f-]{36})", v)
    if m:
        return TargetKind.EVENT, load(TargetKind.EVENT, m.group(1))
    m = re.search(r"/communities/.+/posts/([0-9a-f-]{36})#comment-([0-9a-f-]{36})", v)
    if m:
        return TargetKind.COMMUNITY_COMMENT, load(TargetKind.COMMUNITY_COMMENT, m.group(2))
    m = re.search(r"/communities/.+/posts/([0-9a-f-]{36})", v)
    if m:
        return TargetKind.COMMUNITY_POST, load(TargetKind.COMMUNITY_POST, m.group(1))
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
    for k in (TargetKind.ARTICLE, TargetKind.POST, TargetKind.CANDIDATE, TargetKind.COMMUNITY_POST, TargetKind.COMMUNITY_COMMENT,
              TargetKind.EVENT):
        obj = load(k, u)
        if obj is not None:
            return k, obj
    return None, None
