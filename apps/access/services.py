from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts import clerk
from apps.audit.models import AuditLog
from apps.audit.services import record

from .models import IMPLIED_BY_SYSTEM_ADMIN, Role, RoleAssignment, ScopeType


class RoleError(Exception):
    """A role/deactivation command could not be carried out (message is user-facing)."""


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
def _active(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.is_active)


def is_system_admin(user) -> bool:
    return _active(user) and RoleAssignment.objects.filter(user=user, role=Role.SYSTEM_ADMIN, is_active=True).exists()


def has_role(user, role, scope_type=ScopeType.GLOBAL, scope_id=None) -> bool:
    if not _active(user):
        return False
    if role in IMPLIED_BY_SYSTEM_ADMIN and is_system_admin(user):
        return True
    qs = RoleAssignment.objects.filter(user=user, role=role, is_active=True)
    if scope_type == ScopeType.GLOBAL:
        return qs.filter(scope_type=ScopeType.GLOBAL).exists()
    # A global grant (where the role allows it, e.g. moderator) covers every scope.
    if qs.filter(scope_type=ScopeType.GLOBAL).exists():
        return True
    return qs.filter(scope_type=scope_type, scope_id=scope_id).exists()


# --------------------------------------------------------------------------
# Commands (all audited)
# --------------------------------------------------------------------------
@transaction.atomic
def grant_role(*, user, role, scope_type=ScopeType.GLOBAL, scope_id=None, note="", actor=None, actor_label=""):
    if not user.is_active:
        raise RoleError("Can't grant roles to a deactivated account.")
    if RoleAssignment.objects.filter(user=user, role=role, scope_type=scope_type, scope_id=scope_id, is_active=True).exists():
        raise RoleError("This user already has that role for that scope.")
    assignment = RoleAssignment(
        user=user, role=role, scope_type=scope_type, scope_id=scope_id, note=note,
        granted_by=actor, granted_by_label=actor_label,
    )
    try:
        assignment.full_clean()
    except ValidationError as exc:
        raise RoleError("; ".join(m for msgs in exc.message_dict.values() for m in msgs)) from exc
    assignment.save()
    record(AuditLog.Action.ROLE_GRANTED, target=assignment, actor=actor, actor_label=actor_label,
           user_id=str(user.pk), role=role, scope_type=scope_type, scope_id=str(scope_id) if scope_id else None)
    return assignment


@transaction.atomic
def revoke_role(*, assignment, reason="", actor=None, actor_label=""):
    if not assignment.is_active:
        raise RoleError("That role assignment is already revoked.")
    assignment.is_active = False
    assignment.revoked_at = timezone.now()
    assignment.revoked_by = actor
    assignment.save(update_fields=["is_active", "revoked_at", "revoked_by", "updated_at"])
    record(AuditLog.Action.ROLE_REVOKED, target=assignment, actor=actor, actor_label=actor_label,
           user_id=str(assignment.user_id), role=assignment.role, reason=reason)
    return assignment


def deactivate_user(*, user, reason="", actor=None, actor_label="") -> bool:
    """
    Block API access locally AND ban in Clerk (spec §6.5). Local deactivation always
    holds; if the Clerk call fails it is logged, and re-running retries it.
    Returns True if Clerk confirmed the ban.
    """
    first_time = user.is_active
    if first_time:
        user.is_active = False
        user.deactivated_at = timezone.now()
        user.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    banned = clerk.ban_user(user.clerk_user_id)
    record(AuditLog.Action.USER_DEACTIVATED, target=user, actor=actor, actor_label=actor_label,
           reason=reason, clerk_ban="ok" if banned else "failed", retry=not first_time)
    return banned


def reactivate_user(*, user, reason="", actor=None, actor_label="") -> bool:
    if user.clerk_deleted_at:
        raise RoleError("This account was deleted in Clerk and can't be reactivated.")
    was_inactive = not user.is_active
    if was_inactive:
        user.is_active = True
        user.deactivated_at = None
        user.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    unbanned = clerk.unban_user(user.clerk_user_id)
    record(AuditLog.Action.USER_REACTIVATED, target=user, actor=actor, actor_label=actor_label,
           reason=reason, clerk_unban="ok" if unbanned else "failed", retry=not was_inactive)
    return unbanned
