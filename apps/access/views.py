"""Team page API (spec 11 §4.3, §7): system admins grant and revoke global admin roles."""
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.core.exceptions import ServiceError

from . import services
from .models import Role, RoleAssignment, ScopeType
from .permissions import IsSystemAdmin

User = get_user_model()


class AdminView(APIView):
    """Private, uncached admin endpoint base (spec 11 §3.8)."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class AdminWriteThrottle(UserRateThrottle):
    scope = "admin_write"


class AdminReadThrottle(UserRateThrottle):
    scope = "admin_read"


def role_error(exc: services.RoleError) -> ServiceError:
    return ServiceError(str(exc), code=exc.code, status_code=exc.status)


def _scope_label(a):
    if a.scope_type == ScopeType.GLOBAL:
        return "Everywhere"
    if a.scope_type == ScopeType.CANDIDATE:
        from apps.candidates.models import Candidate

        c = Candidate.objects.filter(pk=a.scope_id).only("full_name").first()
        return c.full_name if c else "A removed candidate"
    return "One community"


def assignment_payload(a, viewer=None):
    who = lambda u, label="": (u.email or "Deleted account") if u else ("System (CLI)" if label == "system:cli" else "System")  # noqa: E731
    return {
        "id": str(a.id), "role": a.role, "role_label": Role(a.role).label, "scope_type": a.scope_type,
        "scope_id": str(a.scope_id) if a.scope_id else None, "scope_label": _scope_label(a),
        "user": {"id": str(a.user_id), "full_name": a.user.full_name, "email": a.user.email, "is_active": a.user.is_active},
        "note": a.note, "granted_by": who(a.granted_by, a.granted_by_label), "granted_at": a.created_at,
        "is_active": a.is_active, "revoked_at": a.revoked_at, "revoked_by": who(a.revoked_by) if a.revoked_at else None,
        "is_own": bool(viewer and a.user_id == viewer.pk),
    }


class TeamView(AdminView):
    permission_classes = [IsSystemAdmin]

    def get_throttles(self):
        return [AdminWriteThrottle()] if self.request.method == "POST" else []

    def get(self, request):
        rows = services.team_list(include_revoked=request.query_params.get("revoked") in ("1", "true"))
        return Response({"results": [assignment_payload(a, request.user) for a in rows], "grantable": services.grantable_roles()})

    def post(self, request):
        email = str(request.data.get("email", "")).strip()
        role = str(request.data.get("role", ""))
        note = str(request.data.get("note", ""))[:255]
        if role not in {r.value for r in services.GRANTABLE}:
            raise ServiceError("Choose one of the roles in the list. Candidate links are made on the candidate page.",
                               code="invalid_role", status_code=400)
        if not email:
            raise ServiceError("Enter the person's email.", code="validation_error", status_code=400)
        user = User.objects.filter(email__iexact=email, clerk_deleted_at__isnull=True).order_by("-created_at").first()
        if user is None:
            raise ServiceError("No NDCConnect account uses that email. They need to sign in once first.",
                               code="user_not_found", status_code=404)
        try:
            a = services.grant_role(user=user, role=role, note=note, actor=request.user)
        except services.RoleError as exc:
            raise role_error(exc) from exc
        return Response(assignment_payload(a, request.user), status=status.HTTP_201_CREATED)


class TeamRevokeView(AdminView):
    permission_classes = [IsSystemAdmin]
    throttle_classes = [AdminWriteThrottle]

    def post(self, request, pk):
        a = get_object_or_404(RoleAssignment.objects.select_related("user"), pk=pk)
        if a.role == Role.CANDIDATE:
            raise ServiceError("Candidate links are managed on the candidate page.", code="invalid_role", status_code=400)
        try:
            services.revoke_role(assignment=a, reason=str(request.data.get("reason", ""))[:300], actor=request.user)
        except services.RoleError as exc:
            raise role_error(exc) from exc
        a.refresh_from_db()
        return Response(assignment_payload(a, request.user))
