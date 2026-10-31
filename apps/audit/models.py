"""Append-only audit trail — spec §5.3 (FR-059, NFR-005)."""
import uuid

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    class Action(models.TextChoices):
        USER_CREATED = "user.created"
        USER_UPDATED = "user.updated"
        USER_DELETED = "user.deleted"
        PROFILE_COMPLETED = "profile.completed"
        PROFILE_UPDATED = "profile.updated"
        ROLE_GRANTED = "role.granted"
        ROLE_REVOKED = "role.revoked"
        USER_DEACTIVATED = "user.deactivated"
        USER_REACTIVATED = "user.reactivated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # For non-user actors: "system:cli", "clerk:webhook", "system:jit".
    actor_label = models.CharField(max_length=32, blank=True)
    action = models.CharField(max_length=40, choices=Action.choices, db_index=True)
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["target_type", "target_id"])]

    def __str__(self):
        who = self.actor_id or self.actor_label or "unknown"
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action} by {who}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise PermissionError("Audit log entries are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit log entries cannot be deleted.")
