"""Communities API — spec 17 §13. Every response is per-user (`private, no-store`, `noindex`); never cached publicly."""
import uuid

from django.db.models import Count, Q
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as DS
from rest_framework import status
from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.audit.models import AuditLog
from apps.audit.services import client_ip, record
from apps.core.exceptions import ServiceError
from apps.news.serializers import ReportSerializer
from apps.news.views import PublicThrottle, ReportThrottle

from . import serializers as P
from . import services
from .models import Community, CommunityComment, CommunityPost, ItemStatus, Level, ReadScope


# ------------------------------------------------------------------ plumbing
def _throttle(scope_name, methods=("POST",)):
    class _T(UserRateThrottle):
        scope = scope_name

        def allow_request(self, request, view):
            return True if request.method not in methods else super().allow_request(request, view)

    _T.__name__ = f"Throttle_{scope_name}"
    return _T


PostHour, PostDay = _throttle("community_post_hour"), _throttle("community_post_day")
CommentThrottle = _throttle("community_comment")
ReactionThrottle = _throttle("community_reaction", ("PUT", "DELETE"))
WriteThrottle = _throttle("community_write", ("POST", "PATCH", "DELETE"))


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        response["X-Robots-Tag"] = "noindex"  # spec 17 §6
        return response


def _uuid(v):
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise ServiceError("Not found.", code="not_found", status_code=404) from None


def _community(pk):
    c = services.load(_uuid(pk))
    if c is None:
        raise ServiceError("Not found.", code="not_found", status_code=404)
    return c


def _require_read(request, c):
    """Guests: 401 (sign in to read). Members-only levels: 404 to everyone who isn't a member, so existence isn't revealed."""
    if services.can_read(request.user, c):
        return
    if c.read_scope == ReadScope.MEMBERS:
        raise ServiceError("Not found.", code="not_found", status_code=404)
    raise ServiceError("Sign in to read community posts.", code="not_authenticated", status_code=401)


def _post(request, pk):
    p = CommunityPost.objects.select_related("community__parent__parent__parent__parent", "author", "campaign_candidate").filter(
        pk=_uuid(pk)).first()
    if p is None or p.status == ItemStatus.DELETED and p.author_id != getattr(request.user, "pk", None):
        raise ServiceError("Not found.", code="not_found", status_code=404)
    _require_read(request, p.community)
    return p


def _comment(request, pk):
    c = CommunityComment.objects.select_related("post__community__parent__parent__parent__parent", "author", "campaign_candidate").filter(
        pk=_uuid(pk)).first()
    if c is None:
        raise ServiceError("Not found.", code="not_found", status_code=404)
    _require_read(request, c.post.community)
    return c


class FeedPagination(CursorPagination):
    page_size = 20
    ordering = ("-created_at", "-id")  # spec 17 §13.3: cursor pages, so new posts don't shift pages


class Page50(PageNumberPagination):
    page_size = 50


# ------------------------------------------------------------------ public structure
class CommunityList(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [PublicThrottle]

    @extend_schema(summary="Communities: Nigeria and the 37 states, or ?parent=<id> for the level below", responses=P.CommunitySchema(many=True),
                   parameters=[OpenApiParameter("parent", str, description="A community ID")])
    def get(self, request):
        parent = request.query_params.get("parent")
        if parent:
            qs = Community.objects.filter(parent_id=_uuid(parent), active=True)
        else:
            qs = Community.objects.filter(level__in=[Level.NATIONAL, Level.STATE], active=True)
        qs = qs.annotate(kids=Count("children", filter=Q(children__active=True))).order_by("level" if not parent else "path", "name")
        rows = sorted(qs, key=lambda c: (c.level != Level.NATIONAL, c.name))
        return Response([P.community(c, has_children=c.kids > 0) for c in rows])


class CommunityLookup(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [PublicThrottle]

    @extend_schema(summary="Find a community by its path (national, lagos, lagos/ikeja, …)", responses=P.CommunityDetailSchema,
                   parameters=[OpenApiParameter("path", str, required=True)])
    def get(self, request):
        path = str(request.query_params.get("path", "")).strip("/").lower()
        c = Community.objects.select_related("parent__parent__parent__parent").filter(path=path).first()
        if c is None or (not c.active and not services.membership(request.user, c)):
            raise ServiceError("Not found.", code="not_found", status_code=404)
        return Response(P.detail(c, request.user))


class CommunityDetail(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [PublicThrottle]

    @extend_schema(summary="One community: breadcrumb, what you can do here, your membership", responses=P.CommunityDetailSchema)
    def get(self, request, pk):
        return Response(P.detail(_community(pk), request.user))


# ------------------------------------------------------------------ posts
class CommunityPosts(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [PostHour, PostDay]

    @extend_schema(summary="Pinned announcements, then posts, newest first (cursor pages of 20)", responses=P.FeedSchema)
    def get(self, request, pk):
        c = _community(pk)
        _require_read(request, c)
        paginator = FeedPagination()
        page = paginator.paginate_queryset(services.feed(c), request, view=self)
        liked = services.liked_ids(request.user, page)
        badges = P.badges_for(page)
        pinned = [P.pinned(a) for a in services.pinned_announcements(c).select_related("audience_community")] \
            if not request.query_params.get("cursor") else []
        return Response({"pinned": pinned, "results": [P.post(p, request.user, liked=p.pk in liked, badges=badges) for p in page],
                         "next": paginator.get_next_link(), "previous": paginator.get_previous_link()})

    @extend_schema(summary="Post in this community", request=P.WritePostSchema, responses={201: P.PostSchema})
    def post(self, request, pk):
        c = _community(pk)
        if not request.user or not request.user.is_authenticated:
            raise ServiceError("Sign in to take part.", code="not_authenticated", status_code=401)
        p = services.create_post(request.user, c, request.data.get("body", ""), request.data.get("campaign_candidate"))
        p = CommunityPost.objects.select_related("community", "author", "campaign_candidate").get(pk=p.pk)
        return Response(P.post(p, request.user, liked=False), status=status.HTTP_201_CREATED)


class PostDetail(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [WriteThrottle]

    @extend_schema(summary="One community post", responses=P.PostSchema)
    def get(self, request, pk):
        return Response(P.post(_post(request, pk), request.user))

    @extend_schema(summary="Edit your post (30 minutes, unless reported)", request=P.WritePostSchema, responses=P.PostSchema)
    def patch(self, request, pk):
        p = services.edit_post(request.user, _post(request, pk), request.data.get("body", ""))
        return Response(P.post(_post(request, p.pk), request.user))

    @extend_schema(summary="Delete your post", responses=P.PostSchema)
    def delete(self, request, pk):
        p = services.delete_post(request.user, _post(request, pk))
        return Response(P.post(_post(request, p.pk), request.user))


class PostComments(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [CommentThrottle]

    @extend_schema(summary="Comments on a post, oldest first (50 a page)",
                   responses=inline_serializer("CommunityCommentPage", {"count": DS.IntegerField(), "next": DS.CharField(allow_null=True),
                                                                         "previous": DS.CharField(allow_null=True),
                                                                         "results": P.CommentSchema(many=True)}))
    def get(self, request, pk):
        p = _post(request, pk)
        if p.status != ItemStatus.PUBLISHED and p.author_id != getattr(request.user, "pk", None):
            qs = p.comments.none()
        else:
            qs = services.visible_comments(p)
        paginator = Page50()
        page = paginator.paginate_queryset(qs, request, view=self)
        badges = P.badges_for(page)
        return paginator.get_paginated_response([P.comment(c, request.user, badges=badges) for c in page])

    @extend_schema(summary="Comment on a post", request=P.WritePostSchema, responses={201: P.CommentSchema})
    def post(self, request, pk):
        if not request.user or not request.user.is_authenticated:
            raise ServiceError("Sign in to take part.", code="not_authenticated", status_code=401)
        c = services.create_comment(request.user, _post(request, pk), request.data.get("body", ""), request.data.get("campaign_candidate"))
        return Response(P.comment(_comment(request, c.pk), request.user), status=status.HTTP_201_CREATED)


class CommentDetail(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [WriteThrottle]

    @extend_schema(summary="Edit your comment (30 minutes, unless reported)", request=P.WritePostSchema, responses=P.CommentSchema)
    def patch(self, request, pk):
        c = services.edit_comment(request.user, _comment(request, pk), request.data.get("body", ""))
        return Response(P.comment(_comment(request, c.pk), request.user))

    @extend_schema(summary="Delete your comment", responses=P.CommentSchema)
    def delete(self, request, pk):
        c = services.delete_comment(request.user, _comment(request, pk))
        return Response(P.comment(_comment(request, c.pk), request.user))


class PostReaction(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [ReactionThrottle]

    @extend_schema(summary="Like a post", request=None, responses=P.ReactionSchema)
    def put(self, request, pk):
        return Response(services.react(request.user, _post(request, pk), True))

    @extend_schema(summary="Remove your like", responses=P.ReactionSchema)
    def delete(self, request, pk):
        return Response(services.react(request.user, _post(request, pk), False))


def _report(request, kind, obj):
    from apps.moderation import services as moderation

    ser = ReportSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    out = moderation.report(kind, obj, ser.validated_data["reason"], ser.validated_data.get("details", ""), user=request.user,
                            ip=client_ip(request) or "")
    return Response(out, status=status.HTTP_200_OK if "route" in out else status.HTTP_201_CREATED)


REPORT_RESPONSE = inline_serializer("CommunityReportResponse", {"reference": DS.CharField(required=False), "route": DS.CharField(required=False)})


class PostReport(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [ReportThrottle]

    @extend_schema(summary="Report a community post (13's reasons)", request=ReportSerializer, responses={201: REPORT_RESPONSE})
    def post(self, request, pk):
        return _report(request, "community_post", _post(request, pk))


class CommentReport(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [ReportThrottle]

    @extend_schema(summary="Report a community comment (13's reasons)", request=ReportSerializer, responses={201: REPORT_RESPONSE})
    def post(self, request, pk):
        return _report(request, "community_comment", _comment(request, pk))


# ------------------------------------------------------------------ /me
class MyCommunities(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Your communities and what's available to you", responses=P.MyCommunitiesSchema)
    def get(self, request):
        return Response(P.my_communities(request.user))


class MyJoin(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [WriteThrottle]

    @extend_schema(summary="Join communities: asks for the communities consent, then places you from your verified PVC (§5.2)",
                   request=None, responses=P.MyCommunitiesSchema)
    def post(self, request):
        from apps.privacy.consent import require_consent

        if not request.user.profile_complete:
            raise ServiceError("Finish your profile first.", code="profile_incomplete", status_code=403)
        require_consent(request.user, services.PURPOSE)
        services.place(request.user)
        return Response(P.my_communities(request.user))


class MyState(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [WriteThrottle]

    @extend_schema(summary="Choose your state community (not yet verified; once every 30 days)",
                   request=inline_serializer("ChooseState", {"state": DS.CharField()}), responses=P.MyCommunitiesSchema)
    def post(self, request):
        services.choose_state(request.user, str(request.data.get("state", "")))
        return Response(P.my_communities(request.user))


class MyRejoin(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [WriteThrottle]

    @extend_schema(summary="Rejoin a level you left", request=inline_serializer("RejoinLevel", {"level": DS.CharField()}),
                   responses=P.MyCommunitiesSchema)
    def post(self, request):
        services.rejoin(request.user, str(request.data.get("level", "")))
        return Response(P.my_communities(request.user))


class MyCommunity(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [WriteThrottle]

    @extend_schema(summary="Turn new-post notifications on or off (LGA, ward and polling unit)",
                   request=inline_serializer("CommunityBell", {"notify_new_posts": DS.BooleanField()}), responses=P.MyCommunitiesSchema)
    def patch(self, request, pk):
        v = request.data.get("notify_new_posts")
        if not isinstance(v, bool):
            raise ServiceError("notify_new_posts must be true or false.", code="validation_error", status_code=400)
        services.set_bell(request.user, _community(pk), v)
        return Response(P.my_communities(request.user))

    @extend_schema(summary="Leave a community (leaving the state also leaves the levels below)", responses=P.MyCommunitiesSchema)
    def delete(self, request, pk):
        services.leave(request.user, _community(pk))
        return Response(P.my_communities(request.user))


# ------------------------------------------------------------------ admin
def _admin_row(c, *, open_cases=0, moderators=None):
    return P.community(c) | {"member_count": str(services.admin_count(c)), "read_scope": c.read_scope, "posting_open": c.posting_open,
                             "ndc_members_only": c.ndc_members_only, "open_cases": open_cases, "moderators": moderators or []}


def _open_cases(c):
    from apps.moderation.models import Case, CaseStatus

    posts = CommunityPost.objects.filter(services.under_paths([c.path], "community__path")).values("pk")
    comments = CommunityComment.objects.filter(services.under_paths([c.path], "post__community__path")).values("pk")
    return Case.objects.exclude(status=CaseStatus.CLOSED).filter(
        Q(target_kind="community_post", target_id__in=posts) | Q(target_kind="community_comment", target_id__in=comments)).count()


class AdminCommunities(_Private):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="Communities tree for admins (?parent=<id>)", responses=P.AdminCommunitySchema(many=True),
                   parameters=[OpenApiParameter("parent", str)])
    def get(self, request):
        parent = request.query_params.get("parent")
        qs = Community.objects.filter(parent_id=_uuid(parent)) if parent else Community.objects.filter(level__in=[Level.NATIONAL, Level.STATE])
        rows = sorted(qs, key=lambda c: (c.level != Level.NATIONAL, c.name))
        return Response([_admin_row(c) for c in rows])


class AdminCommunity(_Private):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="One community: settings, counts, moderators, open cases (audited)", responses=P.AdminCommunitySchema)
    def get(self, request, pk):
        c = _community(pk)
        mods = list(RoleAssignment.objects.filter(role=Role.MODERATOR, scope_type=ScopeType.COMMUNITY, scope_id=c.pk, is_active=True)
                    .values_list("user__email", flat=True))
        record(AuditLog.Action.COMMUNITY_COUNTS_VIEWED, target=c, actor=request.user, request=request)
        return Response(_admin_row(c, open_cases=_open_cases(c), moderators=mods))

    @extend_schema(summary="Change a community's settings", responses=P.AdminCommunitySchema,
                   request=inline_serializer("CommunitySettingsChange", {"posting_open": DS.BooleanField(required=False),
                                                                         "read_scope": DS.ChoiceField(ReadScope.choices, required=False),
                                                                         "ndc_members_only": DS.BooleanField(required=False)}))
    def patch(self, request, pk):
        c = _community(pk)
        changed = []
        for f in ("posting_open", "ndc_members_only"):
            if f in request.data:
                if not isinstance(request.data[f], bool):
                    raise ServiceError(f"{f} must be true or false.", code="validation_error", status_code=400)
                if getattr(c, f) != request.data[f]:
                    setattr(c, f, request.data[f])
                    changed.append(f)
        if "read_scope" in request.data:
            if request.data["read_scope"] not in ReadScope.values:
                raise ServiceError("Choose who can read.", code="validation_error", status_code=400)
            if c.read_scope != request.data["read_scope"]:
                c.read_scope = request.data["read_scope"]
                changed.append("read_scope")
        if changed:
            c.save(update_fields=[*changed, "updated_at"])
            record(AuditLog.Action.COMMUNITY_SETTINGS_CHANGED, target=c, actor=request.user, request=request, fields=changed)
        return self.get(request, pk)
