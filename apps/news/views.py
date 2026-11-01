"""News API — spec 07 §7."""
from django.db.models import Count, Min, Q
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from apps.access.models import Role
from apps.access.permissions import HasRole, IsProfileComplete, IsSignedIn, IsSystemAdmin
from apps.audit.services import client_ip
from apps.candidates.reference import State
from apps.core.exceptions import ServiceError

from . import announcements, reports, services
from .feed import feed
from .images import PhotoError
from .models import Article, ArticleReport, ArticleStatus, ReportStatus, Source
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


def _upload(request):
    upload = request.FILES.get("image")
    if upload is None:
        raise ServiceError("Choose an image to upload.", code="validation_error", status_code=400)
    confirmed = str(request.data.get("rights_confirmed", "")).lower() in ("true", "1", "on", "yes")
    return upload, confirmed


# ------------------------------------------------------------------ public
class NewsList(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request):
        source = request.query_params.get("source", "")
        state = request.query_params.get("state", "")
        if state and state not in State.values:
            state = ""
        pinned, qs = feed(source if source in Source.values else "", state)
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(PublicArticleSerializer(page, many=True, context={"request": request}).data)
        first = paginator.page.number == 1
        response.data["pinned"] = PublicArticleSerializer(pinned, many=True, context={"request": request}).data if first else []
        patch_cache_control(response, public=True, max_age=60)
        return response


class NewsDetail(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request, pk):
        a = get_object_or_404(Article.objects.public().select_related("author"), pk=pk)
        response = Response(PublicArticleDetailSerializer(a, context={"request": request}).data)
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
        reports.report(a, ser.validated_data["reason"], ser.validated_data.get("details", ""), user=user,
                       contact_email="" if user else ser.validated_data.get("contact_email", ""), ip=client_ip(request) or "")
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


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
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [WriteThrottle]

    def put(self, request, pk):
        a = _mine(request, pk)
        upload, confirmed = _upload(request)
        return _image_errors(lambda: Response(MyArticleSerializer(
            services.set_image(a, request.user, upload, rights_confirmed=confirmed, request=request), context={"request": request}).data))

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
    return Article.objects.select_related("author").annotate(open_reports=Count("reports", filter=Q(reports__status=ReportStatus.OPEN)))


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
        q = (qp.get("q") or "").strip()[:100]
        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(author__email__icontains=q))
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminArticleSerializer(page, many=True, context={"request": request}).data)
        open_reports = ArticleReport.objects.filter(status=ReportStatus.OPEN)
        response.data["counts"] = {
            "pending": Article.objects.filter(status=ArticleStatus.PENDING).count(),
            "reported": open_reports.values("article").distinct().count(),
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
            services.remove(a, request.user, reason, request=request)
        elif self.action_name == "restore":
            services.restore(a, request.user, request=request)
        return Response(AdminArticleDetailSerializer(_admin_qs().get(pk=pk), context={"request": request}).data)


class AdminReportDismiss(_Private):
    permission_classes = [Reviewer]

    def post(self, request, pk):
        r = reports.dismiss(get_object_or_404(ArticleReport, pk=pk), request.user, str(request.data.get("note", "")), request=request)
        return Response(AdminArticleDetailSerializer(_admin_qs().get(pk=r.article_id), context={"request": request}).data)


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
    parser_classes = [MultiPartParser, FormParser]

    def put(self, request, pk):
        a = _official(pk)
        upload, confirmed = _upload(request)
        return _image_errors(lambda: (services.set_image(a, request.user, upload, rights_confirmed=confirmed, request=request),
                                      Response(AdminArticleDetailSerializer(_official(pk), context={"request": request}).data))[1])

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
            services.remove(a, request.user, str(request.data.get("reason", "")), request=request)
        elif self.action_name == "restore":
            services.restore(a, request.user, request=request)
        return Response(AdminArticleDetailSerializer(_official(pk), context={"request": request}).data)
