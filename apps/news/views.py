"""News API — spec 07 §7."""
from django.db.models import Min, Q
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import (
    AnonRateThrottle,
    SimpleRateThrottle,
    UserRateThrottle,
)
from rest_framework.views import APIView

from apps.access.models import Role
from apps.access.permissions import (
    HasRole,
    IsProfileComplete,
    IsSignedIn,
    IsSystemAdmin,
)
from apps.audit.services import client_ip
from apps.candidates.reference import State
from apps.core import filters as F
from apps.core.exceptions import ServiceError
from apps.core.i18n import CONTENT_LANGUAGES
from apps.moderation import services as moderation
from apps.moderation.models import Report, ReportStatus

from . import announcements, reports, services
from .feed import feed_page
from .images import PhotoError
from .models import Article, ArticleStatus, Source
from .serializers import (
    AdminArticleDetailSerializer,
    AdminArticleSerializer,
    AnnouncementSerializer,
    EditSerializer,
    MyArticleSerializer,
    PublicArticleDetailSerializer,
    PublicArticleSerializer,
    ReportSerializer,
    SubmitSerializer,
)

Reviewer = HasRole(Role.NEWS_REVIEWER)


class Page20(PageNumberPagination):
    page_size = 20


def _private(response):
    add_never_cache_headers(response)
    patch_cache_control(response, private=True, no_store=True)
    return response


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        return _private(super().finalize_response(request, response, *args, **kwargs))


class PublicThrottle(AnonRateThrottle):
    scope = "news_public"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class SubmitThrottle(UserRateThrottle):
    scope = "news_submit"


class WriteThrottle(UserRateThrottle):
    scope = "news_write"


class ReportThrottle(SimpleRateThrottle):
    """Per user when signed in, per IP for guests (spec 07 §4.6)."""

    scope = "news_report"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        ident = f"u{user.pk}" if user is not None and user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


def _image_errors(fn):
    try:
        return fn()
    except PhotoError as exc:
        return Response({"error": {"status": 400, "code": "invalid_photo", "message": str(exc), "fields": {"image": [str(exc)]}}}, status=400)
    except OSError as exc:
        raise ServiceError("Image storage is unavailable right now. Your article was saved without the new image; try again later.",
                           code="storage_unavailable", status_code=503) from exc


def _upload(request, article):
    from apps.media.http import upload_or_asset

    purpose = "announcement_image" if article.source == "official" else "news_image"
    return upload_or_asset(request, field="image", purpose=purpose, target_id=article.pk)


# ------------------------------------------------------------------ public
class NewsList(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request):
        from apps.posts.models import CandidatePost
        from apps.posts.serializers import FeedPostSerializer

        source = F.choice(request, "source", ["official", "community", "candidate"])
        state = F.choice(request, "state", State.values)
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        language = F.choice(request, "language", list(CONTENT_LANGUAGES))  # spec 21 §9
        pinned, items, count = feed_page(source, state, page, language=language)
        ctx = {"request": request}
        results = [FeedPostSerializer(i, context=ctx).data if isinstance(i, CandidatePost) else PublicArticleSerializer(i, context=ctx).data
                   for i in items]
        if page > 1 and not results:
            raise ServiceError("Invalid page.", code="not_found", status_code=404)
        response = Response({"count": count, "next": None, "previous": None, "results": results,
                             "pinned": PublicArticleSerializer(pinned, many=True, context=ctx).data if page == 1 else []})
        patch_cache_control(response, public=True, max_age=60)
        return response


class NewsDetail(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request, pk):
        a = Article.objects.public().select_related("author", "audience_community").filter(pk=pk).first()
        if a is None:
            moderation.raise_if_removed("article", Article.objects.filter(pk=pk, status=ArticleStatus.REMOVED).first())
            raise ServiceError("Not found.", code="not_found", status_code=404)
        data = PublicArticleDetailSerializer(a, context={"request": request}).data
        from .translations import public_versions

        data |= public_versions(a, request.query_params.get("lang", ""))  # spec 21 §10: ?lang= picks a published translation
        response = Response(data)
        patch_cache_control(response, public=True, max_age=60)
        return response


class NewsReport(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [ReportThrottle]

    def post(self, request, pk):
        a = get_object_or_404(Article.objects.public(), pk=pk)
        ser = ReportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user if request.user and request.user.is_authenticated else None
        out = reports.report(a, ser.validated_data["reason"], ser.validated_data.get("details", ""), user=user,
                             contact_email="" if user else ser.validated_data.get("contact_email", ""), ip=client_ip(request) or "")
        return Response(out, status=status.HTTP_200_OK if "route" in out else status.HTTP_201_CREATED)


# ------------------------------------------------------------------ member
def _mine(request, pk):
    return get_object_or_404(Article, pk=pk, author=request.user, source=Source.COMMUNITY)


class MyNews(_Private):
    def get_permissions(self):
        return [IsSignedIn()] if self.request.method == "GET" else [IsProfileComplete()]

    def get_throttles(self):
        return [SubmitThrottle()] if self.request.method == "POST" else []

    def get(self, request):
        qs = Article.objects.filter(author=request.user, source=Source.COMMUNITY).exclude(status=ArticleStatus.WITHDRAWN, title="")
        paginator = Page20()
        page = paginator.paginate_queryset(qs.order_by("-created_at"), request, view=self)
        return paginator.get_paginated_response(MyArticleSerializer(page, many=True, context={"request": request}).data)

    def post(self, request):
        ser = SubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        a = services.submit(request.user, ser.validated_data, request=request)
        return Response(MyArticleSerializer(a, context={"request": request}).data, status=status.HTTP_201_CREATED)


class MyNewsDetail(_Private):
    def get_permissions(self):
        return [IsSignedIn()] if self.request.method == "GET" else [IsProfileComplete()]

    def get_throttles(self):
        return [WriteThrottle()] if self.request.method != "GET" else []

    def get(self, request, pk):
        return Response(MyArticleSerializer(_mine(request, pk), context={"request": request}).data)

    def patch(self, request, pk):
        ser = EditSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        a = services.edit(_mine(request, pk), request.user, ser.validated_data, request=request)
        return Response(MyArticleSerializer(a, context={"request": request}).data)


class MyNewsImage(_Private):
    permission_classes = [IsProfileComplete]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_classes = [WriteThrottle]

    def put(self, request, pk):
        a = _mine(request, pk)

        def run():
            upload, confirmed = _upload(request, a)
            return Response(MyArticleSerializer(services.set_image(a, request.user, upload, rights_confirmed=confirmed, request=request),
                                                context={"request": request}).data)

        return _image_errors(run)

    def delete(self, request, pk):
        a = services.remove_image(_mine(request, pk), request.user, request=request)
        return Response(MyArticleSerializer(a, context={"request": request}).data)


class MyNewsWithdraw(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [WriteThrottle]

    def post(self, request, pk):
        a = services.withdraw(_mine(request, pk), request.user, request=request)
        return Response(MyArticleSerializer(a, context={"request": request}).data)


# ---------------------------------------------------------------- reviewer
def _admin_qs():
    return Article.objects.select_related("author", "audience_community").annotate(open_reports=moderation.open_reports_subquery("article"))


class AdminNewsList(_Private):
    permission_classes = [Reviewer]

    def get(self, request):
        qp = request.query_params
        qs = _admin_qs().exclude(status=ArticleStatus.WITHDRAWN)
        if qp.get("reported") in ("1", "true"):
            qs = qs.filter(open_reports__gt=0).order_by("created_at")
        elif qp.get("status") in ArticleStatus.values:
            qs = qs.filter(status=qp["status"])
            qs = qs.order_by("submitted_at") if qp["status"] == ArticleStatus.PENDING else qs.order_by("-published_at", "-created_at")
        else:
            qs = qs.order_by("-created_at")
        if qp.get("source") in Source.values:
            qs = qs.filter(source=qp["source"])
        if qp.get("language") in CONTENT_LANGUAGES:  # spec 21 §9
            qs = qs.filter(language=qp["language"])
        q = (qp.get("q") or "").strip()[:100]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(author__email__icontains=q))
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminArticleSerializer(page, many=True, context={"request": request}).data)
        open_reports = Report.objects.filter(target_kind="article", status=ReportStatus.OPEN)
        response.data["counts"] = {
            "pending": Article.objects.filter(status=ArticleStatus.PENDING).count(),
            "reported": open_reports.values("target_id").distinct().count(),
            "oldest_open_report": open_reports.aggregate(t=Min("created_at"))["t"],
            "paused_until": services.paused_until(),
        }
        return response


class AdminNewsDetail(_Private):
    permission_classes = [Reviewer]

    def get(self, request, pk):
        return Response(AdminArticleDetailSerializer(get_object_or_404(_admin_qs(), pk=pk), context={"request": request}).data)


class AdminNewsAction(_Private):
    permission_classes = [Reviewer]
    action_name = ""

    def post(self, request, pk):
        a = get_object_or_404(Article, pk=pk)
        reason = str(request.data.get("reason", ""))
        if self.action_name == "approve":
            services.approve(a, request.user, request=request)
        elif self.action_name == "reject":
            services.reject(a, request.user, reason, request=request)
        elif self.action_name == "remove":
            services.remove(a, request.user, reason, request=request, rule=str(request.data.get("rule", "")),
                            note_to_author=str(request.data.get("note_to_author", ""))[:500])
        elif self.action_name == "restore":
            services.restore(a, request.user, request=request, note=str(request.data.get("note", ""))[:1000])
        return Response(AdminArticleDetailSerializer(_admin_qs().get(pk=pk), context={"request": request}).data)


class AdminReportDismiss(_Private):
    permission_classes = [Reviewer]

    def post(self, request, pk):
        r = get_object_or_404(Report, pk=pk, target_kind="article")
        reports.dismiss(r, request.user, str(request.data.get("note", "")), request=request)
        return Response(AdminArticleDetailSerializer(_admin_qs().get(pk=r.target_id), context={"request": request}).data)


# ----------------------------------------------------------- announcements
def _official(pk):
    return get_object_or_404(_admin_qs(), pk=pk, source=Source.OFFICIAL)


class AdminAnnouncementList(_Private):
    permission_classes = [IsSystemAdmin]
    parser_classes = [JSONParser]

    def get(self, request):
        qs = _admin_qs().filter(source=Source.OFFICIAL).order_by("-published_at")
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminArticleSerializer(page, many=True, context={"request": request}).data)
        response.data["paused_until"] = services.paused_until()
        response.data["pinned_count"] = Article.objects.filter(source=Source.OFFICIAL, pinned_at__isnull=False).count()
        return response

    def post(self, request):
        ser = AnnouncementSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        notify = data.pop("notify", False)
        a = announcements.publish(request.user, data, notify=notify, request=request)
        return Response(AdminArticleDetailSerializer(_official(a.pk), context={"request": request}).data, status=status.HTTP_201_CREATED)


class AnnouncementNotifyCount(_Private):
    permission_classes = [IsSystemAdmin]

    def get(self, request):
        from apps.notifications.services import eligible_for_announcements

        return Response({"count": eligible_for_announcements().count()})


class AdminAnnouncementDetail(_Private):
    permission_classes = [IsSystemAdmin]

    def get(self, request, pk):
        return Response(AdminArticleDetailSerializer(_official(pk), context={"request": request}).data)

    def patch(self, request, pk):
        ser = AnnouncementSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = {k: v for k, v in ser.validated_data.items() if k != "notify"}  # edits never notify (spec 07 §4.7)
        announcements.update(_official(pk), request.user, data, request=request)
        return Response(AdminArticleDetailSerializer(_official(pk), context={"request": request}).data)


class AnnouncementImage(_Private):
    permission_classes = [IsSystemAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def put(self, request, pk):
        a = _official(pk)

        def run():
            upload, confirmed = _upload(request, a)
            services.set_image(a, request.user, upload, rights_confirmed=confirmed, request=request)
            return Response(AdminArticleDetailSerializer(_official(pk), context={"request": request}).data)

        return _image_errors(run)

    def delete(self, request, pk):
        services.remove_image(_official(pk), request.user, request=request)
        return Response(AdminArticleDetailSerializer(_official(pk), context={"request": request}).data)


class AnnouncementAction(_Private):
    permission_classes = [IsSystemAdmin]
    action_name = ""

    def post(self, request, pk):
        a = _official(pk)
        if self.action_name == "pin":
            announcements.pin(a, request.user, request=request)
        elif self.action_name == "unpin":
            announcements.unpin(a, request.user, request=request)
        elif self.action_name == "unpublish":
            services.remove(a, request.user, str(request.data.get("reason", "")), request=request, rule=str(request.data.get("rule", "")),
                            note_to_author=str(request.data.get("note_to_author", ""))[:500])
        elif self.action_name == "restore":
            services.restore(a, request.user, request=request, note=str(request.data.get("note", ""))[:1000])
        return Response(AdminArticleDetailSerializer(_official(pk), context={"request": request}).data)


# ------------------------------------------------------------------ spec 21 §10: versions in other languages
VERSION = inline_serializer("AnnouncementVersion", {
    "language": serializers.CharField(), "title": serializers.CharField(), "body": serializers.CharField(),
    "status": serializers.ChoiceField(["draft", "published"]), "out_of_date": serializers.BooleanField(),
    "translated_by": serializers.CharField(), "reviewed_by": serializers.CharField(),
    "published_at": serializers.DateTimeField(allow_null=True), "updated_at": serializers.DateTimeField()})
VERSION_IN = inline_serializer("AnnouncementVersionIn", {"title": serializers.CharField(), "body": serializers.CharField()})
TRANSLATIONS = inline_serializer("AnnouncementVersions", {"language": serializers.CharField(), "versions": VERSION.__class__(many=True)})
class AnnouncementTranslations(_Private):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="An announcement's versions in other languages", responses=TRANSLATIONS)
    def get(self, request, pk):
        from . import translations as T

        a = T._official(pk)
        return Response({"language": a.language, "versions": [T.payload(t, a) for t in a.translations.select_related(
            "translated_by", "reviewed_by")]})


class AnnouncementTranslation(_Private):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="Write or change the version in one language (goes back to draft; audited)", request=VERSION_IN,
                   responses=VERSION)
    def put(self, request, pk, code):
        from . import translations as T

        t = T.save(pk, code, request.user, request.data, request=request)
        return Response(T.payload(t))

    @extend_schema(summary="Delete the version in one language (audited)", responses={204: None})
    def delete(self, request, pk, code):
        from . import translations as T

        T.delete(pk, code, request.user, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AnnouncementTranslationPublish(_Private):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="Review and publish a version someone else wrote (audited)", request=None, responses=VERSION)
    def post(self, request, pk, code):
        from . import translations as T

        return Response(T.payload(T.publish(pk, code, request.user, request=request)))
