"""
/api/v1/me/follows/… — spec 04 §7. The caller's own data only.

- IsSignedIn and nothing more: never IsProfileComplete, never a PVC check (§3).
- Every response is `Cache-Control: private, no-store`.
- Acts only on request.user; there is no user_id parameter anywhere (§9).
"""
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn

from . import services
from .serializers import FollowSerializer, NotifySerializer

MAX_SLUGS = 50


class FollowWriteThrottle(UserRateThrottle):
    """60/min per user on PUT/PATCH/DELETE; reads aren't throttled here (§7.5)."""

    scope = "follows"

    def allow_request(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return super().allow_request(request, view)


class Page20(PageNumberPagination):
    page_size = 20


class _Private(APIView):
    permission_classes = [IsSignedIn]
    throttle_classes = [FollowWriteThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class MyFollows(_Private):
    """GET — the Following page, or follow state for `?slugs=a,b,c` (≤ 50)."""

    def get(self, request):
        raw = request.query_params.get("slugs")
        if raw is not None:
            slugs = [s.strip() for s in raw.split(",") if s.strip()]
            if len(slugs) > MAX_SLUGS:
                raise ValidationError({"slugs": [f"Ask for at most {MAX_SLUGS} candidates at a time."]})
            return Response({"results": services.states_for(request.user, slugs)})

        paginator = Page20()
        page = paginator.paginate_queryset(services.visible_follows(request.user), request, view=self)
        response = paginator.get_paginated_response(FollowSerializer(page, many=True, context={"request": request}).data)
        response.data["hidden_count"] = services.hidden_count(request.user)
        return response


class MyFollowDetail(_Private):
    """PUT follow · PATCH notify · DELETE unfollow, by candidate slug."""

    def put(self, request, slug):
        candidate = services.published_candidate(slug)
        f, created = services.follow(request.user, candidate)
        data = FollowSerializer(f, context={"request": request}).data
        return Response(data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    def patch(self, request, slug):
        candidate = services.published_candidate(slug)
        ser = NotifySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        f = services.set_notify(request.user, candidate, ser.validated_data["notify"])
        return Response(FollowSerializer(f, context={"request": request}).data)

    def delete(self, request, slug):
        services.unfollow(request.user, slug)
        return Response(status=status.HTTP_204_NO_CONTENT)
