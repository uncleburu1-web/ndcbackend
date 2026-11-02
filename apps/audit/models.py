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
        CANDIDATE_CREATED = "candidate.created"
        CANDIDATE_UPDATED = "candidate.updated"
        CANDIDATE_VERIFIED = "candidate.verified"
        CANDIDATE_UNVERIFIED = "candidate.unverified"
        CANDIDATE_PUBLISHED = "candidate.published"
        CANDIDATE_UNPUBLISHED = "candidate.unpublished"
        CANDIDATE_ARCHIVED = "candidate.archived"
        CANDIDATE_RESTORED = "candidate.restored"
        CANDIDATE_PHOTO_UPDATED = "candidate.photo_updated"
        DONATION_VIEWED = "donation.viewed"
        DONATION_EXPORTED = "donation.exported"
        DONATION_RECHECKED = "donation.rechecked"
        DONATION_LABEL_ANONYMISED = "donation.label_anonymised"
        PVC_SUBMITTED = "pvc.submitted"
        PVC_VERIFIED = "pvc.verified"
        PVC_REJECTED = "pvc.rejected"
        PVC_VIN_REVEALED = "pvc.vin_revealed"
        MEMBERSHIP_SUBMITTED = "membership.submitted"
        MEMBERSHIP_PAYMENT_STARTED = "membership.payment_started"
        MEMBERSHIP_ACTIVATED = "membership.activated"
        MEMBERSHIP_APPROVED = "membership.approved"
        MEMBERSHIP_REJECTED = "membership.rejected"
        MEMBERSHIP_CANCELLED = "membership.cancelled"
        MEMBERSHIP_NIN_REVEALED = "membership.nin_revealed"
        MEMBERSHIP_PHOTO_VIEWED = "membership.photo_viewed"
        MEMBERSHIP_REGISTER_EXPORTED = "membership.register_exported"
        MEMBERSHIP_SETTINGS_CHANGED = "membership.settings_changed"
        MEMBERSHIP_ERASED = "membership.erased_on_account_deletion"
        MEMBERSHIP_PAYMENT_RECHECKED = "membership.payment_rechecked"
        NEWS_SUBMITTED = "news.submitted"
        NEWS_EDITED = "news.edited"
        NEWS_WITHDRAWN = "news.withdrawn"
        NEWS_APPROVED = "news.approved"
        NEWS_REJECTED = "news.rejected"
        NEWS_REMOVED = "news.removed"
        NEWS_RESTORED = "news.restored"
        NEWS_REPORT_RESOLVED = "news.report_resolved"
        NEWS_ERASED = "news.erased_on_account_deletion"
        ANNOUNCEMENT_PUBLISHED = "announcement.published"
        ANNOUNCEMENT_UPDATED = "announcement.updated"
        ANNOUNCEMENT_UNPUBLISHED = "announcement.unpublished"
        ANNOUNCEMENT_RESTORED = "announcement.restored"
        ANNOUNCEMENT_PINNED = "announcement.pinned"
        ANNOUNCEMENT_DELIVERED = "announcement.delivered"
        PVC_SETTINGS_CHANGED = "pvc.settings_changed"
        POST_CREATED = "post.created"
        POST_UPDATED = "post.updated"
        POST_IMAGE_UPDATED = "post.image_updated"
        POST_DELETED = "post.deleted"
        POST_REMOVED = "post.removed"
        POST_RESTORED = "post.restored"
        POST_REPORT_RESOLVED = "post.report_resolved"
        POST_DELIVERED = "post.delivered"
        USER_VIEWED = "user.viewed"  # spec 11: account page opened by a system admin
        USER_PHONE_REVEALED = "user.phone_revealed"
        AUDIT_VIEWED = "audit.viewed"  # spec 11: the audit log itself was searched

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # For non-user actors: "system:cli", "clerk:webhook", "system:jit".
    actor_label = models.CharField(max_length=32, blank=True)
    action = models.CharField(max_length=48, choices=Action.choices, db_index=True)
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
