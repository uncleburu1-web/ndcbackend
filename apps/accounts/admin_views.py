"""Admin users API (spec 11 §4.4, §7). System admins only; account facts only."""
from django.shortcuts import get_object_or_404
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.access import services as access
from apps.access.models import RoleAssignment
from apps.access.permissions import IsSystemAdmin
from apps.access.views import (
    AdminReadThrottle,
    AdminView,
    AdminWriteThrottle,
    assignment_payload,
    role_error,
)
from apps.audit.display import entry_payload
from apps.audit.models import AuditLog
from apps.core.exceptions import ServiceError

from . import services
from .models import User


class Page20(PageNumberPagination):
    page_size = 20


def summary(u: User) -> dict:
    deleted = u.clerk_deleted_at is not None
    return {
        "id": str(u.id), "full_name": "" if deleted else u.full_name, "email": "" if deleted else u.email,
        "joined_at": u.created_at, "profile_complete": u.profile_complete, "is_active": u.is_active, "deleted": deleted,
        # spec 20 §14: Active, Paused by the person, or Deactivated by an admin. Nothing else from Settings.
        "deactivation_kind": u.deactivation_kind,
        "roles": [{"role": a.role, "scope_type": a.scope_type} for a in u.role_assignments.all() if a.is_active],
    }


def detail(u: User, viewer) -> dict:
    data = summary(u)
    last_deact = (AuditLog.objects.filter(action=AuditLog.Action.USER_DEACTIVATED, target_id=str(u.pk))
                  .select_related("actor").order_by("-created_at").first())
    data.update(
        phone_masked=services.masked_phone(u.phone_number) if not data["deleted"] else "",
        updated_at=u.updated_at, deactivated_at=u.deactivated_at, clerk_deleted_at=u.clerk_deleted_at,
        deactivation=None if u.is_active or not last_deact else {
            "at": last_deact.created_at, "by": entry_payload(last_deact)["actor"], "reason": last_deact.metadata.get("reason", ""),
            "clerk_ban": last_deact.metadata.get("clerk_ban", "")},
        assignments=[assignment_payload(a, viewer) for a in RoleAssignment.objects.filter(user=u)
                     .select_related("user", "granted_by", "revoked_by").order_by("-is_active", "-created_at")],
        recent_actions=[entry_payload(e) for e in AuditLog.objects.filter(actor=u).exclude(action__startswith="audit.")
                        .select_related("actor").order_by("-created_at")[:20]],
        is_self=u.pk == viewer.pk,
    )
    return data


class UsersView(AdminView):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminReadThrottle]

    def get(self, request):
        q = request.query_params.get("q", "").strip()
        flt = request.query_params.get("filter", "")
        if len(q) < 3 and flt != "ban_failed":
            raise ServiceError("Type at least 3 characters of a name or email.", code="query_too_short", status_code=400)
        qs = services.admin_search(q, request.query_params.get("status", ""), flt).prefetch_related("role_assignments")
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response([summary(u) for u in page])


class UserDetailView(AdminView):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminReadThrottle]

    def get(self, request, pk):
        u = get_object_or_404(User.objects.prefetch_related("role_assignments"), pk=pk)
        services.record_view_once(AuditLog.Action.USER_VIEWED, target=u, viewer=request.user, request=request)
        return Response(detail(u, request.user))


class UserPhoneView(AdminView):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminWriteThrottle]

    def post(self, request, pk):
        u = get_object_or_404(User, pk=pk, clerk_deleted_at__isnull=True)
        from apps.audit.services import record

        record(AuditLog.Action.USER_PHONE_REVEALED, target=u, actor=request.user, request=request)
        return Response({"phone_number": u.phone_number})


class UserActionView(AdminView):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminWriteThrottle]
    action = ""

    def post(self, request, pk):
        u = get_object_or_404(User.objects.prefetch_related("role_assignments"), pk=pk)
        reason = str(request.data.get("reason", "")).strip()[:300]
        try:
            if self.action == "deactivate":
                if not reason:
                    raise ServiceError("Give a reason for deactivating this account.", code="validation_error", status_code=400)
                ok = access.deactivate_user(user=u, reason=reason, actor=request.user)
                result = {"clerk_ban": "ok" if ok else "failed"}
            else:
                ok = access.reactivate_user(user=u, reason=reason, actor=request.user)
                result = {"clerk_unban": "ok" if ok else "failed"}
        except access.RoleError as exc:
            raise role_error(exc) from exc
        u.refresh_from_db()
        return Response({"user": detail(User.objects.prefetch_related("role_assignments").get(pk=u.pk), request.user), **result})
