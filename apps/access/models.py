"""
Scoped RBAC — spec §5.2 (FR-060, NFR-003).

Roles live ONLY here. Clerk metadata is never consulted for authorization.
"Verified voter" is not a role: it is derived from PVC status in a later spec
and never grants admin rights (SRS 2.2.3). Assignments are never deleted.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class Role(models.TextChoices):
    SYSTEM_ADMIN = "system_admin", "System administrator"
    MODERATOR = "moderator", "Community moderator"
    NEWS_REVIEWER = "news_reviewer", "News reviewer"
    CANDIDATE = "candidate", "Candidate"
    MEMBERSHIP_OFFICER = "membership_officer", "Membership officer"
    FINANCE_OFFICER = "finance_officer", "Finance officer"  # spec 05: donation records + exports only
    PVC_OFFICER = "pvc_officer", "PVC officer"  # spec 08: the PVC check queue only


class ScopeType(models.TextChoices):
    GLOBAL = "global", "Global"
    COMMUNITY = "community", "Community"
    CANDIDATE = "candidate", "Candidate record"


ALLOWED_SCOPES: dict[str, set[str]] = {
    Role.SYSTEM_ADMIN: {ScopeType.GLOBAL},
    Role.MODERATOR: {ScopeType.GLOBAL, ScopeType.COMMUNITY},
    Role.NEWS_REVIEWER: {ScopeType.GLOBAL},
    Role.CANDIDATE: {ScopeType.CANDIDATE},
    Role.MEMBERSHIP_OFFICER: {ScopeType.GLOBAL},
    Role.FINANCE_OFFICER: {ScopeType.GLOBAL},
    Role.PVC_OFFICER: {ScopeType.GLOBAL},
}

# system_admin passes these checks too. It does NOT imply `candidate`:
# an admin doesn't publish as a candidate.
IMPLIED_BY_SYSTEM_ADMIN = {Role.MODERATOR, Role.NEWS_REVIEWER, Role.MEMBERSHIP_OFFICER, Role.FINANCE_OFFICER, Role.PVC_OFFICER}


class RoleAssignment(TimeStampedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.CharField(max_length=32, choices=Role.choices)
    scope_type = models.CharField(max_length=16, choices=ScopeType.choices, default=ScopeType.GLOBAL)
    scope_id = models.UUIDField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    granted_by_label = models.CharField(max_length=32, blank=True)  # e.g. "system:cli"
    is_active = models.BooleanField(default=True, db_index=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scope_type=ScopeType.GLOBAL, scope_id__isnull=True)
                    | (~Q(scope_type=ScopeType.GLOBAL) & Q(scope_id__isnull=False))
                ),
                name="access_roleassignment_scope_consistent",
            ),
            models.UniqueConstraint(
                fields=["user", "role", "scope_type", "scope_id"],
                condition=Q(is_active=True),
                nulls_distinct=False,
                name="access_roleassignment_unique_active",
            ),
        ]
        indexes = [models.Index(fields=["user", "role", "is_active"])]

    def __str__(self):
        scope = "global" if self.scope_type == ScopeType.GLOBAL else f"{self.scope_type}:{self.scope_id}"
        return f"{self.user} → {self.role} @ {scope}"

    def clean(self):
        allowed = ALLOWED_SCOPES.get(self.role, set())
        if self.scope_type not in allowed:
            raise ValidationError({"scope_type": f"'{self.role}' can only be granted at: {', '.join(sorted(allowed))}."})
        if self.scope_type == ScopeType.GLOBAL and self.scope_id:
            raise ValidationError({"scope_id": "Global roles must not have a scope id."})
        if self.scope_type != ScopeType.GLOBAL and not self.scope_id:
            raise ValidationError({"scope_id": "This scope needs a scope id."})
