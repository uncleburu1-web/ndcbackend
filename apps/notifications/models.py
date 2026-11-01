"""In-app notifications — spec 07 §5.4. 08 adds pvc_decision; 09 adds candidate_post."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Kind(models.TextChoices):
    ANNOUNCEMENT = "announcement", "Announcement"
    NEWS_DECISION = "news_decision", "News decision"
    PVC_DECISION = "pvc_decision", "PVC decision"  # spec 08: no FK and no PVC data in the row


class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    article = models.ForeignKey("news.Article", null=True, blank=True, on_delete=models.CASCADE, related_name="notifications")
    decision = models.CharField(max_length=10, blank=True)  # approved | rejected (news) · verified | rejected (pvc)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["recipient", "article"], name="notif_one_announcement",
                                    condition=Q(kind="announcement")),
            models.CheckConstraint(name="notif_article_by_kind",
                                   condition=(Q(kind__in=["announcement", "news_decision"], article__isnull=False)
                                              | Q(kind="pvc_decision", article__isnull=True))),
        ]
        indexes = [
            models.Index(fields=["recipient", "-created_at"], name="notif_recipient_idx"),
            models.Index(fields=["recipient"], name="notif_unread_idx", condition=Q(read_at__isnull=True)),
        ]


class NotificationPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preference")
    announcements = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
