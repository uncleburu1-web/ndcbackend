"""
DRF permission classes — spec §6.4. Hiding a button in the UI is never the security boundary.

IsProfileComplete is for features that need a real name and phone (news submission,
membership). It must NEVER be used on candidate, follow, notification or donation
endpoints, and nothing here checks PVC status (architecture invariant).
"""
from rest_framework.permissions import BasePermission

from .models import ScopeType
from .services import has_role, is_system_admin


class IsSignedIn(BasePermission):
    """Valid Clerk session + active local user."""

    message = "Sign in to continue."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_active)


class IsProfileComplete(IsSignedIn):
    message = "Finish setting up your profile first."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.profile_complete


class IsSystemAdmin(IsSignedIn):
    message = "System administrator access is required."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and is_system_admin(request.user)


def HasRole(role, scope_type=ScopeType.GLOBAL, scope_kwarg=None):
    """
    Scoped check for later specs, e.g.
        permission_classes = [HasRole(Role.CANDIDATE, ScopeType.CANDIDATE, "candidate_id")]
    The scope id is read from the URL kwarg `scope_kwarg`.
    """

    class _HasRole(IsSignedIn):
        message = "You don't have permission to do this."

        def has_permission(self, request, view):
            if not super().has_permission(request, view):
                return False
            scope_id = view.kwargs.get(scope_kwarg) if scope_kwarg else None
            return has_role(request.user, role, scope_type, scope_id)

    _HasRole.__name__ = f"HasRole_{role}_{scope_type}"
    return _HasRole
