from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts import clerk
from apps.audit.models import AuditLog
from apps.audit.services import record

from .models import IMPLIED_BY_SYSTEM_ADMIN, Role, RoleAssignment, ScopeType


class RoleError(Exception):
    """A role/deactivation command could not be carried out (message is user-facing).

    `code` and `status` let API views turn it into the error envelope (spec 11 §7)."""

    def __init__(self, message, code="role_error", status=409):
        super().__init__(message)
        self.code, self.status = code, status


SELF_CHANGE = "You can't change your own access. Ask another system administrator."
LAST_ADMIN = "There must always be at least one system administrator. Grant the role to someone else first."


def _guard_self(user, actor):
    """Browser calls pass the signed-in admin as `actor`; nobody changes their own access (spec 11 §3.4).
    CLI calls have no actor, so the owner can still bootstrap the first admin."""
    if actor is not None and getattr(actor, "pk", None) == user.pk:
        raise RoleError(SELF_CHANGE, code="self_change", status=403)


def _guard_last_admin(user):
    """Refuse to leave zero active system admins (spec 11 §3.5). Locks every active system admin
    assignment so two admins removing each other at the same time can't both succeed."""
    active = list(RoleAssignment.objects.select_for_update().filter(role=Role.SYSTEM_ADMIN, is_active=True, user__is_active=True)
                  .values_list("user_id", flat=True))
    if user.pk in active and len(set(active)) <= 1:
        raise RoleError(LAST_ADMIN, code="last_system_admin", status=409)


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
    _guard_self(user, actor)
    if not user.is_active:
        raise RoleError("Can't grant roles to a deactivated account.", code="user_inactive")
    if RoleAssignment.objects.filter(user=user, role=role, scope_type=scope_type, scope_id=scope_id, is_active=True).exists():
        raise RoleError("This user already has that role for that scope.", code="role_exists")
    assignment = RoleAssignment(
        user=user, role=role, scope_type=scope_type, scope_id=scope_id, note=note,
        granted_by=actor, granted_by_label=actor_label,
    )
    try:
        assignment.full_clean()
    except ValidationError as exc:
        raise RoleError("; ".join(m for msgs in exc.message_dict.values() for m in msgs)) from exc
    assignment.save()
    from .security import forget_admin

    forget_admin(user)
    record(AuditLog.Action.ROLE_GRANTED, target=assignment, actor=actor, actor_label=actor_label,
           user_id=str(user.pk), role=role, scope_type=scope_type, scope_id=str(scope_id) if scope_id else None)
    return assignment


@transaction.atomic
@transaction.atomic
def revoke_role(*, assignment, reason="", actor=None, actor_label=""):
    _guard_self(assignment.user, actor)
    if not assignment.is_active:
        raise RoleError("That role assignment is already revoked.", code="invalid_transition")
    if assignment.role == Role.SYSTEM_ADMIN:
        _guard_last_admin(assignment.user)
    assignment.is_active = False
    assignment.revoked_at = timezone.now()
    assignment.revoked_by = actor
    assignment.save(update_fields=["is_active", "revoked_at", "revoked_by", "updated_at"])
    from .security import forget_admin

    forget_admin(assignment.user)
    record(AuditLog.Action.ROLE_REVOKED, target=assignment, actor=actor, actor_label=actor_label,
           user_id=str(assignment.user_id), role=assignment.role, reason=reason)
    return assignment


def deactivate_user(*, user, reason="", actor=None, actor_label="") -> bool:
    """
    Block API access locally AND ban in Clerk (spec §6.5). Local deactivation always
    holds; if the Clerk call fails it is logged, and re-running retries it.
    Returns True if Clerk confirmed the ban.
    """
    _guard_self(user, actor)
    first_time = user.is_active
    if first_time:
        with transaction.atomic():
            _guard_last_admin(user)
            user.is_active = False
            user.deactivated_at = timezone.now()
            user.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    banned = clerk.ban_user(user.clerk_user_id)
    record(AuditLog.Action.USER_DEACTIVATED, target=user, actor=actor, actor_label=actor_label,
           reason=reason, clerk_ban="ok" if banned else "failed", retry=not first_time)
    return banned


def reactivate_user(*, user, reason="", actor=None, actor_label="") -> bool:
    _guard_self(user, actor)
    if user.clerk_deleted_at:
        raise RoleError("This account was deleted in Clerk and can't be reactivated.", code="invalid_transition")
    was_inactive = not user.is_active
    if was_inactive:
        user.is_active = True
        user.deactivated_at = None
        user.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    unbanned = clerk.unban_user(user.clerk_user_id)
    record(AuditLog.Action.USER_REACTIVATED, target=user, actor=actor, actor_label=actor_label,
           reason=reason, clerk_unban="ok" if unbanned else "failed", retry=not was_inactive)
    return unbanned


# --------------------------------------------------------------------------
# Team page (spec 11 §4.3, §6.4)
# --------------------------------------------------------------------------
ROLE_ORDER = [Role.SYSTEM_ADMIN, Role.NEWS_REVIEWER, Role.PVC_OFFICER, Role.MEMBERSHIP_OFFICER, Role.FINANCE_OFFICER,
              Role.MODERATOR, Role.PRIVACY_OFFICER, Role.CANDIDATE]
ROLE_DESCRIPTIONS = {
    Role.SYSTEM_ADMIN: "manage every admin area, people, roles and settings",
    Role.NEWS_REVIEWER: "approve or reject community news and act on reports about community news",
    Role.PVC_OFFICER: "check PVC details against INEC's voter checker",
    Role.MEMBERSHIP_OFFICER: "check PVCs, approve memberships and export the register",
    Role.FINANCE_OFFICER: "see donation records and export them",
    Role.MODERATOR: "review reports on community news and candidate posts, remove or restore content, warn or pause authors",
    Role.PRIVACY_OFFICER: "handle privacy requests, incidents and the data register",
    Role.CANDIDATE: "post for one candidate (linked on the candidate page)",
}
# Granted from the browser. `candidate` needs the candidate record (03). `moderator` became grantable with spec 13.
GRANTABLE = [Role.SYSTEM_ADMIN, Role.NEWS_REVIEWER, Role.PVC_OFFICER, Role.MEMBERSHIP_OFFICER, Role.FINANCE_OFFICER, Role.MODERATOR,
             Role.PRIVACY_OFFICER]


def grantable_roles() -> list[dict]:
    return [{"role": r.value, "label": r.label, "description": ROLE_DESCRIPTIONS[r]} for r in GRANTABLE]


def team_list(include_revoked=False):
    qs = RoleAssignment.objects.select_related("user", "granted_by", "revoked_by")
    if not include_revoked:
        qs = qs.filter(is_active=True)
    order = {r.value: i for i, r in enumerate(ROLE_ORDER)}
    return sorted(qs, key=lambda a: (order.get(a.role, 99), not a.is_active, -a.created_at.timestamp()))
