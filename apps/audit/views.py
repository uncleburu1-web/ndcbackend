"""Audit log viewer API (spec 11 §4.5, §7). System admins only; every search is itself audited."""
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.access.permissions import IsSystemAdmin
from apps.access.views import AdminReadThrottle, AdminView
from apps.accounts.services import record_view_once

from . import services
from .display import ACTIONS, AREAS, entry_payload
from .models import AuditLog


class Page50(PageNumberPagination):
    page_size = 50


class AuditSearch(AdminView):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminReadThrottle]

    def get(self, request):
        qs, stored = services.search(request.query_params)
        record_view_once(AuditLog.Action.AUDIT_VIEWED, target=None, viewer=request.user, request=request, filters=stored)
        paginator = Page50()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response([entry_payload(e, full=True) for e in page])


class AuditActions(AdminView):
    permission_classes = [IsSystemAdmin]

    def get(self, request):
        return Response({"areas": [
            {"area": key, "label": label,
             "actions": [{"action": a, "label": ACTIONS[a][0]} for a in ACTIONS if any(a.startswith(p) for p in prefixes)]}
            for key, (label, prefixes) in AREAS.items()]})
