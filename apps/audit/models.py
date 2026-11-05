"""Append-only audit trail — spec §5.3 (FR-059, NFR-005)."""
import uuid

from django.conf import settings
from django.db import models


class AuditLog(models.Model):
    """Append-only, hash-chained record of sensitive actions (FR-059; spec 02, 14). Kept 6 years."""

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
        # spec 13: reporting and moderation
        MODERATION_CASE_CLAIMED = "moderation.case_claimed"
        MODERATION_KEPT = "moderation.kept"
        MODERATION_REMOVED = "moderation.removed"
        MODERATION_RESTORED = "moderation.restored"
        MODERATION_ESCALATED = "moderation.escalated"
        MODERATION_WARNED = "moderation.warned"
        MODERATION_PAUSED = "moderation.paused"
        MODERATION_PAUSE_LIFTED = "moderation.pause_lifted"
        MODERATION_APPEAL_DECIDED = "moderation.appeal_decided"
        MODERATION_REPORTER_VIEWED = "moderation.reporter_viewed"
        MODERATION_LEGAL_NOTICE_LOGGED = "moderation.legal_notice_logged"
        MODERATION_LEGAL_NOTICE_UPDATED = "moderation.legal_notice_updated"
        MODERATION_LEGAL_HOLD_CHANGED = "moderation.legal_hold_changed"
        MODERATION_ATTACHMENT_VIEWED = "moderation.attachment_viewed"
        MODERATION_COMPLIANCE_EXPORTED = "moderation.compliance_exported"
        # spec 14: security and privacy
        PRIVACY_CONSENT_WITHDRAWN = "privacy.consent_withdrawn"
        PRIVACY_REQUEST_VIEWED = "privacy.request_viewed"
        PRIVACY_REQUEST_CLOSED = "privacy.request_closed"
        PRIVACY_DATA_EXPORTED_BY_OFFICER = "privacy.data_exported_by_officer"
        PRIVACY_ACCOUNT_DELETED_SELF = "privacy.account_deleted_self"
        PRIVACY_LINKED_ACTION = "privacy.linked_action"
        SECURITY_INCIDENT_LOGGED = "security.incident_logged"
        SECURITY_INCIDENT_UPDATED = "security.incident_updated"
        SECURITY_INCIDENT_CLOSED = "security.incident_closed"
        SECURITY_NOTICE_SENT = "security.notice_sent"
        SECURITY_INCIDENTS_EXPORTED = "security.incidents_exported"
        SECURITY_ACCESS_REVIEW_COMPLETED = "security.access_review_completed"
        SECURITY_ADMIN_ACCESS_DENIED = "security.admin_access_denied"
        SECURITY_RESTORE_DRILL = "security.restore_drill"
        # Spec 15
        SECURITY_WEBHOOK_SIGNATURE_FAILURES = "security.webhook_signature_failures"
        INTEGRATION_JOB_RETRIED = "integration.job_retried"
        # Spec 17: communities (posting, commenting and liking are never audited: political-opinion data)
        MODERATION_HIDDEN = "moderation.hidden"
        MODERATION_UNHIDDEN = "moderation.unhidden"
        COMMUNITY_SETTINGS_CHANGED = "community.settings_changed"
        COMMUNITY_COUNTS_VIEWED = "community.counts_viewed"
        # spec 18 §15.4 (registrations and applications themselves are never audited)
        EVENT_PUBLISHED = "event.published"
        EVENT_CANCELLED = "event.cancelled"
        EVENT_CHECKIN_LIST_OPENED = "event.checkin_list_opened"
        EVENT_PARTY_CREATED = "event.party_created"
        VOLUNTEER_CONTACT_VIEWED = "volunteer.contact_viewed"
        VOLUNTEER_OPENING_CHANGED = "volunteer.opening_changed"

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
    # Spec 14 §4.8: tamper-evident hash chain. Assigned in services.record() under an advisory lock.
    seq = models.BigIntegerField(null=True, blank=True, unique=True)
    prev_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64, blank=True)

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
