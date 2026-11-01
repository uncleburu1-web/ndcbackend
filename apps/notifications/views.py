"""Notifications API — spec 07 §7.5. Acts only on request.user; private, no-store."""
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn
from apps.core.exceptions import ServiceError

from . import services


class _Private(APIView):
    permission_classes = [IsSignedIn]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class Page20(PageNumberPagination):
    page_size = 20


def item(n):
    a = n.article
    return {"id": n.id, "kind": n.kind, "created_at": n.created_at, "read_at": n.read_at, "decision": n.decision,
            "article": {"id": a.id, "source": a.source, "title": a.title, "public": a.status == "successful"}}


class NotificationList(_Private):
    def get(self, request):
        paginator = Page20()
        page = paginator.paginate_queryset(services.visible(request.user).order_by("-created_at"), request, view=self)
        return paginator.get_paginated_response([item(n) for n in page])


class UnreadCount(_Private):
    def get(self, request):
        return Response({"unread": services.unread_count(request.user)})


class MarkRead(_Private):
    def post(self, request, pk):
        from .models import Notification

        if not Notification.objects.filter(recipient=request.user, pk=pk).exists():
            raise ServiceError("Notification not found.", code="not_found", status_code=404)
        services.mark_read(request.user, pk)
        return Response({"ok": True})


class MarkAllRead(_Private):
    def post(self, request):
        return Response({"updated": services.mark_all_read(request.user)})


class Preferences(_Private):
    def get(self, request):
        return Response(services.preferences(request.user))

    def patch(self, request):
        value = request.data.get("announcements")
        if not isinstance(value, bool):
            raise ServiceError("announcements must be true or false.", code="validation_error", status_code=400)
        return Response(services.set_preferences(request.user, value))
