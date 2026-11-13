"""Candidate posts API — spec 09 §7."""
from django.db.models import Min
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.audit.services import client_ip
from apps.candidates.models import Candidate
from apps.core.exceptions import ServiceError
from apps.moderation import services as moderation
from apps.moderation.models import Report, ReportStatus
from apps.news.images import PhotoError
from apps.news.serializers import ReportSerializer
from apps.news.views import PublicThrottle, ReportThrottle

from . import reports, services
from .models import CandidatePost, PostStatus
from .serializers import (
    AdminPostDetailSerializer,
    AdminPostSerializer,
    CampaignPostSerializer,
    PostWriteSerializer,
    PublicPostDetailSerializer,
    PublicPostSerializer,
    candidate_summary,
)


class Page10(PageNumberPagination):
    page_size = 10


class Page20(PageNumberPagination):
    page_size = 20


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class WriteThrottle(UserRateThrottle):
    scope = "posts_write"


def _public_candidate(slug):
    return get_object_or_404(Candidate, slug=slug, status="published", verified_at__isnull=False)


# ------------------------------------------------------------------ public
class PublicPostList(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request, slug):
        c = _public_candidate(slug)
        qs = CandidatePost.objects.visible().filter(candidate=c).order_by("-published_at", "-id")
        paginator = Page10()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(PublicPostSerializer(page, many=True, context={"request": request}).data)
        patch_cache_control(response, public=True, max_age=60)
        return response


class PublicPostDetail(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request, slug, pk):
        p = CandidatePost.objects.visible().select_related("candidate").filter(pk=pk, candidate__slug=slug).first()
        if p is None:
            gone = CandidatePost.objects.filter(pk=pk, candidate__slug=slug, status=PostStatus.REMOVED).select_related("candidate").first()
            if gone is not None and services.candidate_published(gone.candidate):
                moderation.raise_if_removed("post", gone)
            raise ServiceError("Not found.", code="not_found", status_code=404)
        response = Response(PublicPostDetailSerializer(p, context={"request": request}).data)
        patch_cache_control(response, public=True, max_age=60)
        return response


class PostReportView(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [ReportThrottle]

    def post(self, request, slug, pk):
        p = get_object_or_404(CandidatePost.objects.visible(), pk=pk, candidate__slug=slug)
        ser = ReportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user if request.user and request.user.is_authenticated else None
        out = reports.report(p, ser.validated_data["reason"], ser.validated_data.get("details", ""), user=user,
                             contact_email="" if user else ser.validated_data.get("contact_email", ""), ip=client_ip(request) or "")
        return Response(out, status=status.HTTP_200_OK if "route" in out else status.HTTP_201_CREATED)


# ---------------------------------------------------------------- campaign
def _linked(request, candidate_id):
    c = get_object_or_404(Candidate, pk=candidate_id)
    if not services.can_post(request.user, c):
        raise ServiceError("You're not linked to this candidate.", code="permission_denied", status_code=403)
    return c


def _campaign_post(request, candidate_id, post_id):
    c = _linked(request, candidate_id)
    return get_object_or_404(CandidatePost.objects.select_related("candidate"), pk=post_id, candidate=c)


def _campaign_candidate(c, request):
    until = services.paused_until(c)
    return {**candidate_summary(c, request), "status": c.status, "published": services.candidate_published(c),
            "notify_count": services.notify_count(c), "posting_paused_until": until}


class CampaignCandidates(_Private):
    permission_classes = [IsSignedIn]

    def get(self, request):
        return Response({"results": [_campaign_candidate(c, request) for c in services.linked_candidates(request.user)]})


class CampaignPosts(_Private):
    permission_classes = [IsSignedIn]

    def get_throttles(self):
        return [WriteThrottle()] if self.request.method == "POST" else []

    def get(self, request, candidate_id):
        c = _linked(request, candidate_id)
        qs = CandidatePost.objects.filter(candidate=c).exclude(status=PostStatus.DELETED).select_related("candidate").order_by("-published_at")
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(CampaignPostSerializer(page, many=True, context={"request": request}).data)
        response.data["candidate"] = _campaign_candidate(c, request)
        return response

    def post(self, request, candidate_id):
        c = get_object_or_404(Candidate, pk=candidate_id)
        ser = PostWriteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        p = services.create_post(c, request.user, ser.validated_data, request=request)
        return Response(CampaignPostSerializer(p, context={"request": request}).data, status=status.HTTP_201_CREATED)


class CampaignPostDetail(_Private):
    permission_classes = [IsSignedIn]

    def get_throttles(self):
        return [WriteThrottle()] if self.request.method != "GET" else []

    def get(self, request, candidate_id, post_id):
        p = _campaign_post(request, candidate_id, post_id)
        if p.status == PostStatus.DELETED:
            raise ServiceError("Post not found.", code="not_found", status_code=404)
        return Response(CampaignPostSerializer(p, context={"request": request}).data)

    def patch(self, request, candidate_id, post_id):
        ser = PostWriteSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        p = services.update_post(_campaign_post(request, candidate_id, post_id), request.user, ser.validated_data, request=request)
        return Response(CampaignPostSerializer(p, context={"request": request}).data)

    def delete(self, request, candidate_id, post_id):
        services.delete_post(_campaign_post(request, candidate_id, post_id), request.user, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CampaignPostImage(_Private):
    permission_classes = [IsSignedIn]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    throttle_classes = [WriteThrottle]

    def put(self, request, candidate_id, post_id):
        from apps.media.http import upload_or_asset

        p = _campaign_post(request, candidate_id, post_id)
        try:
            upload, confirmed = upload_or_asset(request, field="image", purpose="post_image", target_id=p.pk)
            p = services.set_image(p, request.user, upload, rights_confirmed=confirmed, request=request)
        except PhotoError as exc:
            return Response({"error": {"status": 400, "code": "invalid_photo", "message": str(exc), "fields": {"image": [str(exc)]}}}, status=400)
        except OSError as exc:
            raise ServiceError("Image storage is unavailable right now. Your post was saved without the new image; try again later.",
                               code="storage_unavailable", status_code=503) from exc
        return Response(CampaignPostSerializer(p, context={"request": request}).data)

    def delete(self, request, candidate_id, post_id):
        p = services.remove_image(_campaign_post(request, candidate_id, post_id), request.user, request=request)
        return Response(CampaignPostSerializer(p, context={"request": request}).data)


# ------------------------------------------------------------------- admin
def _admin_qs():
    return (CandidatePost.objects.select_related("candidate", "created_by", "updated_by")
            .annotate(open_reports=moderation.open_reports_subquery("post")))


class AdminPosts(_Private):
    permission_classes = [IsSystemAdmin]

    def get(self, request):
        qp = request.query_params
        qs = _admin_qs().exclude(status=PostStatus.DELETED) if qp.get("status") != "deleted" else _admin_qs()
        if qp.get("reported") in ("1", "true"):
            qs = qs.filter(open_reports__gt=0).order_by("created_at")
        else:
            if qp.get("status") in PostStatus.values:
                qs = qs.filter(status=qp["status"])
            qs = qs.order_by("-published_at")
        if qp.get("candidate"):
            try:
                qs = qs.filter(candidate_id=qp["candidate"])
            except Exception:  # pragma: no cover
                pass
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminPostSerializer(page, many=True, context={"request": request}).data)
        open_reports = Report.objects.filter(target_kind="post", status=ReportStatus.OPEN)
        response.data["counts"] = {"reported": open_reports.values("target_id").distinct().count(),
                                   "oldest_open_report": open_reports.aggregate(t=Min("created_at"))["t"]}
        return response


class AdminPostDetail(_Private):
    permission_classes = [IsSystemAdmin]

    def get(self, request, pk):
        return Response(AdminPostDetailSerializer(get_object_or_404(_admin_qs(), pk=pk), context={"request": request}).data)


class AdminPostAction(_Private):
    permission_classes = [IsSystemAdmin]
    action_name = ""

    def post(self, request, pk):
        p = get_object_or_404(CandidatePost.objects.select_related("candidate"), pk=pk)
        if self.action_name == "remove":
            services.remove_post(p, request.user, str(request.data.get("reason", "")), request=request, rule=str(request.data.get("rule", "")),
                                 note_to_author=str(request.data.get("note_to_author", ""))[:500])
        else:
            services.restore_post(p, request.user, request=request, note=str(request.data.get("note", ""))[:1000])
        return Response(AdminPostDetailSerializer(_admin_qs().get(pk=pk), context={"request": request}).data)


class AdminReportDismiss(_Private):
    permission_classes = [IsSystemAdmin]

    def post(self, request, pk):
        r = get_object_or_404(Report, pk=pk, target_kind="post")
        reports.dismiss(r, request.user, str(request.data.get("note", "")), request=request)
        return Response(AdminPostDetailSerializer(_admin_qs().get(pk=r.target_id), context={"request": request}).data)


class AdminCandidatePosts(_Private):
    permission_classes = [IsSystemAdmin]

    def get(self, request, pk):
        c = get_object_or_404(Candidate, pk=pk)
        qs = _admin_qs().filter(candidate=c).order_by("-published_at")
        return Response({"results": AdminPostSerializer(qs, many=True, context={"request": request}).data})
