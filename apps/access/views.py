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


# --------------------------------------------------------------------------
# Access review (spec 14 §4.6)
# --------------------------------------------------------------------------
def review_rows(viewer):
    from django.conf import settings as dj
    from django.utils import timezone

    now = timezone.now()
    rows = []
    for a in services.team_list():
        if a.role == Role.CANDIDATE:
            continue
        u = a.user
        inactive = u.last_seen_at is None or (now - u.last_seen_at).days >= dj.ADMIN_INACTIVE_DAYS
        rows.append({**assignment_payload(a, viewer), "last_seen_at": u.last_seen_at, "mfa_enabled": u.mfa_enabled, "inactive": inactive})
    return rows


def review_status():
    from django.utils import timezone

    from apps.privacy.models import AccessReview

    last = AccessReview.objects.order_by("-completed_at").first()
    from apps.privacy.dashboard import access_review_due

    due = access_review_due(timezone.now())
    return {"last_review": {"completed_at": last.completed_at, "by": last.completed_by.email if last and last.completed_by else None,
                            "kept": last.kept, "revoked": last.revoked} if last else None, "due": due}


class TeamReviewView(AdminView):
    permission_classes = [IsSystemAdmin]

    def get_throttles(self):
        return [AdminWriteThrottle()] if self.request.method == "POST" else [AdminReadThrottle()]

    def get(self, request):
        return Response({**review_status(), "results": review_rows(request.user)})

    def post(self, request):
        from apps.audit.models import AuditLog
        from apps.audit.services import record
        from apps.privacy.models import AccessReview

        try:
            kept, revoked = int(request.data.get("kept", 0)), int(request.data.get("revoked", 0))
        except (TypeError, ValueError):
            kept, revoked = 0, 0
        r = AccessReview.objects.create(completed_by=request.user, kept=max(0, kept), revoked=max(0, revoked),
                                        notes=str(request.data.get("notes", "") or "")[:1000])
        record(AuditLog.Action.SECURITY_ACCESS_REVIEW_COMPLETED, target=r, actor=request.user, request=request, kept=r.kept, revoked=r.revoked)
        return Response(review_status(), status=status.HTTP_201_CREATED)
