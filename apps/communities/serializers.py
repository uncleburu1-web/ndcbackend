"""Communities API shapes (spec 17 §13). Payloads are built by the functions below; the serializer classes document them for OpenAPI."""
from django.utils import timezone
from rest_framework import serializers as S

from . import services
from .models import LOWER_LEVELS, CommunityPost, ItemStatus, Level
from .text import short_name


# ------------------------------------------------------------------ OpenAPI shapes
class CommunityBriefSchema(S.Serializer):
    id = S.UUIDField()
    level = S.ChoiceField(Level.choices)
    level_label = S.CharField()
    name = S.CharField()
    path = S.CharField()


class CommunitySchema(CommunityBriefSchema):
    state = S.CharField()
    parent_id = S.UUIDField(allow_null=True)
    active = S.BooleanField()
    members = S.CharField(help_text="Banded: 'Fewer than 10', '10–99', '100–999', '1,000+' (spec 17 §4.4)")
    post_count = S.IntegerField()
    has_children = S.BooleanField()


class BlockSchema(S.Serializer):
    code = S.CharField()
    message = S.CharField()
    until = S.CharField(required=False)


class MembershipSchema(S.Serializer):
    how = S.CharField()
    how_label = S.CharField()
    joined_at = S.DateTimeField()
    notify_new_posts = S.BooleanField()


class CommunityDetailSchema(CommunitySchema):
    breadcrumb = CommunityBriefSchema(many=True)
    read_scope = S.CharField()
    posting_open = S.BooleanField()
    ndc_members_only = S.BooleanField()
    can_read = S.BooleanField()
    can_post = S.BooleanField()
    post_block = BlockSchema(allow_null=True)
    membership = MembershipSchema(allow_null=True)
    campaigns = S.ListField(child=S.DictField(), help_text="Linked campaigns to post for (rule 7): [{id, name}]")


class AuthorSchema(S.Serializer):
    name = S.CharField(help_text="First name and last initial, or 'Former member'")
    verified = S.BooleanField()
    campaign = S.CharField(allow_null=True, help_text="'Campaign team · <candidate>' or null")
    is_you = S.BooleanField()


class NoticeSchema(S.Serializer):
    kind = S.ChoiceField(["hidden", "removed", "deleted"])
    rule = S.CharField()
    rule_title = S.CharField()


class CommentSchema(S.Serializer):
    id = S.UUIDField()
    body = S.CharField()
    status = S.CharField()
    created_at = S.DateTimeField()
    edited_at = S.DateTimeField(allow_null=True)
    author = AuthorSchema()
    can_edit = S.BooleanField()
    can_delete = S.BooleanField()
    notice = NoticeSchema(allow_null=True)


class PostSchema(CommentSchema):
    community = CommunityBriefSchema()
    comment_count = S.IntegerField()
    reaction_count = S.IntegerField()
    liked = S.BooleanField()


class PinnedSchema(S.Serializer):
    id = S.UUIDField()
    title = S.CharField()
    category_label = S.CharField()
    published_at = S.DateTimeField()
    audience = CommunityBriefSchema()


class FeedSchema(S.Serializer):
    pinned = PinnedSchema(many=True)
    results = PostSchema(many=True)
    next = S.CharField(allow_null=True)
    previous = S.CharField(allow_null=True)


class MyMembershipSchema(S.Serializer):
    community = CommunitySchema()
    level = S.CharField()
    how = S.CharField()
    how_label = S.CharField()
    joined_at = S.DateTimeField()
    notify_new_posts = S.BooleanField()


class MyCommunitiesSchema(S.Serializer):
    consented = S.BooleanField()
    verified = S.BooleanField()
    memberships = MyMembershipSchema(many=True)
    left_levels = S.ListField(child=S.CharField())
    state_change_available_at = S.DateTimeField(allow_null=True)
    unmatched = S.BooleanField(help_text="Verified, but the polling unit isn't matched to INEC's list yet (§18)")
    geography_loaded = S.BooleanField()


class AdminCommunitySchema(CommunitySchema):
    member_count = S.CharField(help_text="Exact for national, state and LGA; banded below (§4.4)")
    read_scope = S.CharField()
    posting_open = S.BooleanField()
    ndc_members_only = S.BooleanField()
    open_cases = S.IntegerField()
    moderators = S.ListField(child=S.CharField(), help_text="Emails of moderators scoped to this community")


class WritePostSchema(S.Serializer):
    body = S.CharField(max_length=4000)
    campaign_candidate = S.UUIDField(required=False, allow_null=True)


class ReactionSchema(S.Serializer):
    liked = S.BooleanField()
    reaction_count = S.IntegerField()


# ------------------------------------------------------------------ payloads
def brief(c):
    return {"id": c.id, "level": c.level, "level_label": c.get_level_display(), "name": c.name, "path": c.path}


def community(c, *, has_children=None):
    out = brief(c) | {"state": c.state, "parent_id": c.parent_id, "active": c.active, "members": services.band(c.member_count),
                      "post_count": c.post_count}
    out["has_children"] = has_children if has_children is not None else (c.level != Level.POLLING_UNIT and c.children.filter(active=True).exists())
    return out


def detail(c, user):
    from .services import post_block

    out = community(c)
    crumbs = list(reversed(services.chain(c)))
    m = services.membership(user, c)
    block = post_block(user, c)
    linked = services.linked_candidates(user) if services._signed_in(user) else []
    out |= {"breadcrumb": [brief(x) for x in crumbs], "read_scope": c.read_scope, "posting_open": c.posting_open,
            "ndc_members_only": c.ndc_members_only, "can_read": services.can_read(user, c), "can_post": block is None,
            "post_block": ({"code": block[0], "message": block[1], **({"until": block[2]["until"]} if "until" in block[2] else {})}
                           if block else None),
            "membership": ({"how": m.how, "how_label": m.get_how_display(), "joined_at": m.joined_at, "notify_new_posts": m.notify_new_posts}
                           if m else None),
            "campaigns": [{"id": str(x.pk), "name": x.full_name} for x in linked]}
    return out


def _author(obj, user, badges):
    a = obj.author
    info = badges.get(a.pk, {}) if a else {}
    # §5.4: someone who withdrew the communities consent keeps their posts, shown as "Former member" (no badge either).
    former = bool(a) and info.get("former", False)
    return {"name": "Former member" if former else short_name(a), "verified": bool(info.get("badge")) and not former,
            "campaign": f"Campaign team · {obj.campaign_candidate.full_name}" if obj.campaign_candidate_id else None,
            "is_you": bool(user and getattr(user, "pk", None) and obj.author_id == user.pk)}


def _notice(obj):
    from apps.moderation.rules import rule_title

    if obj.status == ItemStatus.HIDDEN:
        return {"kind": "hidden", "rule": "", "rule_title": ""}
    if obj.status == ItemStatus.REMOVED:
        return {"kind": "removed", "rule": obj.removed_rule, "rule_title": rule_title(obj.removed_rule) if obj.removed_rule else ""}
    if obj.status == ItemStatus.DELETED:
        return {"kind": "deleted", "rule": "", "rule_title": ""}
    return None


def badges_for(objs) -> dict:
    """user_id → {badge: shows the verified badge in communities (08's opt-in plus badge consent v2, spec 17 §17.1),
    former: the communities consent has been withdrawn (§5.4)}."""
    from apps.pvc.services import community_badge

    out = {}
    for o in objs:
        if o.author_id and o.author_id not in out:
            out[o.author_id] = {"badge": community_badge(o.author), "former": not services.has_consent(o.author)}
    return out


def _common(obj, user, badges, kind):
    now = timezone.now()
    mine = bool(user and getattr(user, "pk", None) and obj.author_id == user.pk)
    published = obj.status == ItemStatus.PUBLISHED
    editable = mine and published and (now - obj.created_at).total_seconds() < services.EDIT_MINUTES * 60
    return {"id": obj.id, "body": obj.body if published else "", "status": obj.status, "created_at": obj.created_at,
            "edited_at": obj.edited_at, "author": _author(obj, user, badges), "can_edit": editable,
            "can_delete": mine and obj.status in (ItemStatus.PUBLISHED, ItemStatus.HIDDEN), "notice": _notice(obj)}


def post(p: CommunityPost, user, *, liked=None, badges=None):
    badges = badges if badges is not None else badges_for([p])
    out = _common(p, user, badges, "community_post")
    out |= {"community": brief(p.community), "comment_count": p.comment_count, "reaction_count": p.reaction_count,
            "liked": bool(liked) if liked is not None else p.pk in services.liked_ids(user, [p])}
    return out


def comment(c, user, *, badges=None):
    badges = badges if badges is not None else badges_for([c])
    return _common(c, user, badges, "community_comment")


def pinned(a):
    c = a.audience_community
    return {"id": a.id, "title": a.title, "category_label": a.get_category_display() if a.category else "", "published_at": a.published_at,
            "audience": brief(c)}


def my_communities(user):
    from apps.geo.models import PollingUnit
    from apps.pvc.services import is_verified, verified_location

    st = services.CommunitySettings.objects.filter(user=user).first()
    ms = list(services.memberships(user).order_by("community__path"))
    verified = is_verified(user)
    loc = verified_location(user) if verified else None
    change_at = None
    if st and st.state_changed_at:
        change_at = st.state_changed_at + timezone.timedelta(days=services.STATE_CHANGE_DAYS)
        if change_at <= timezone.now():
            change_at = None
    left = [lv for lv in (Level.STATE, *LOWER_LEVELS) if st and getattr(st, f"left_{lv}")]
    return {"consented": services.has_consent(user), "verified": verified,
            "memberships": [{"community": community(m.community), "level": m.level, "how": m.how, "how_label": m.get_how_display(),
                             "joined_at": m.joined_at, "notify_new_posts": m.notify_new_posts} for m in ms],
            "left_levels": left, "state_change_available_at": change_at,
            "unmatched": bool(verified and loc and loc[3] is None), "geography_loaded": PollingUnit.objects.exists()}
