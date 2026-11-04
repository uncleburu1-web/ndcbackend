"""In-app notifications — spec 07 §5.4; pvc_decision from 08; candidate_post from 09."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Kind(models.TextChoices):
    ANNOUNCEMENT = "announcement", "Announcement"
    NEWS_DECISION = "news_decision", "News decision"
    PVC_DECISION = "pvc_decision", "PVC decision"  # spec 08: no FK and no PVC data in the row
    CANDIDATE_POST = "candidate_post", "Candidate post"  # spec 09: follow data — never audited
    # spec 13 §4.6: moderation outcomes. Linked to the case (and appeal), never to the moderator.
    REPORT_OUTCOME = "report_outcome", "Report outcome"
    CONTENT_REMOVED = "content_removed", "Content removed"
    ACCOUNT_WARNING = "account_warning", "Account warning"
    SUBMISSIONS_PAUSED = "submissions_paused", "Submissions paused"
    APPEAL_DECISION = "appeal_decision", "Appeal decision"
    # spec 14: security notices and privacy request updates
    SECURITY_NOTICE = "security_notice", "Security notice"
    PRIVACY_REQUEST_UPDATE = "privacy_request_update", "Privacy request update"
    # spec 15 §9.2
    MEMBERSHIP_DECISION = "membership_decision", "Membership decision"
    DONATION_RECEIPT = "donation_receipt", "Donation receipt"


MODERATION_KINDS = ["report_outcome", "content_removed", "account_warning", "submissions_paused", "appeal_decision"]
PRIVACY_KINDS = ["security_notice", "privacy_request_update"]
OWN_RECORD_KINDS = ["membership_decision", "donation_receipt"]  # spec 15: about the person's own record; no amounts in the row


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    article = models.ForeignKey("news.Article", null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    candidate_post = models.ForeignKey("posts.CandidatePost", null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    case = models.ForeignKey("moderation.Case", null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    appeal = models.ForeignKey("moderation.Appeal", null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    data_request = models.ForeignKey("privacy.DataRequest", null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    incident = models.ForeignKey("privacy.Incident", null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    membership = models.ForeignKey("membership.Membership", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    donation = models.ForeignKey("donations.Donation", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decision = models.CharField(max_length=10, blank=True)  # approved | rejected (news) · verified | rejected (pvc)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["recipient", "article"], name="notif_one_announcement",
                                    condition=Q(kind="announcement")),
            models.UniqueConstraint(fields=["recipient", "candidate_post"], name="notif_one_candidate_post",
                                    condition=Q(kind="candidate_post")),
            models.UniqueConstraint(fields=["recipient", "donation"], name="notif_one_donation_receipt",
                                    condition=Q(kind="donation_receipt")),
            models.CheckConstraint(name="notif_target_by_kind",
                                   condition=(Q(kind__in=["announcement", "news_decision"], article__isnull=False, candidate_post__isnull=True)
                                              | Q(kind="pvc_decision", article__isnull=True, candidate_post__isnull=True)
                                              | Q(kind="candidate_post", candidate_post__isnull=False, article__isnull=True)
                                              | Q(kind__in=MODERATION_KINDS, article__isnull=True, candidate_post__isnull=True)
                                              | Q(kind__in=PRIVACY_KINDS, article__isnull=True, candidate_post__isnull=True)
                                              | Q(kind__in=OWN_RECORD_KINDS, article__isnull=True, candidate_post__isnull=True))),
        ]
        indexes = [
            models.Index(fields=["recipient", "-created_at"], name="notif_recipient_idx"),
            models.Index(fields=["recipient"], name="notif_unread_idx", condition=Q(read_at__isnull=True)),
        ]


class NotificationPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference")
    announcements = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
