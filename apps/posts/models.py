"""
Candidate posts — spec 09 §5. Linked campaign accounts publish directly: there is no Pending status, ever.
"Visible" = published post of a published, verified candidate (CandidatePost.objects.visible()).
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.news.models import ReportReason, ReportStatus


class PostKind(models.TextChoices):
    ANNOUNCEMENT = "announcement", "Announcement"
    NEWS = "news", "News"


class PostStatus(models.TextChoices):
    PUBLISHED = "published", "Published"
    DELETED = "deleted", "Deleted"
    REMOVED = "removed", "Removed"


class PostQuerySet(models.QuerySet):
    def visible(self):
        return self.filter(status=PostStatus.PUBLISHED, candidate__status="published", candidate__verified_at__isnull=False)


class CandidatePost(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey("candidates.Candidate", on_delete=models.PROTECT, related_name="posts")
    kind = models.CharField(max_length=12, choices=PostKind.choices)
    title = models.CharField(max_length=150)
    body = models.TextField(max_length=10_000)
    image = models.CharField(max_length=255, blank=True)
    image_thumb = models.CharField(max_length=255, blank=True)
    image_width = models.PositiveIntegerField(null=True, blank=True)
    image_height = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=PostStatus.choices, default=PostStatus.PUBLISHED)
    published_at = models.DateTimeField()
    edited_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PostQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at"]
        indexes = [
            models.Index(fields=["candidate", "status", "-published_at"], name="posts_candidate_idx"),
            models.Index(fields=["status", "-published_at"], name="posts_feed_idx"),
        ]

    def __str__(self):
        return f"{self.candidate_id}:{self.title[:40]}"


class PostEvent(models.Model):
    """Append-only history (spec 09 §5.2)."""

    class Kind(models.TextChoices):
        PUBLISHED = "published"
        EDITED = "edited"
        IMAGE_CHANGED = "image_changed"
        DELETED = "deleted"
        REMOVED = "removed"
        RESTORED = "restored"
        REPORT_DISMISSED = "report_dismissed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(CandidatePost, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=20, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("PostEvent is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("PostEvent is append-only.")


class PostReport(models.Model):
    """Same shape as news.ArticleReport (spec 09 §5.3); reasons shared with 07."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(CandidatePost, on_delete=models.CASCADE, related_name="reports")
    reason = models.CharField(max_length=12, choices=ReportReason.choices)
    details = models.CharField(max_length=1000, blank=True)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    contact_email = models.EmailField(blank=True)
    ip_hash = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=ReportStatus.choices, default=ReportStatus.OPEN)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["post", "reporter"], name="posts_one_open_report_per_user",
                                    condition=Q(status="open", reporter__isnull=False)),
        ]
        indexes = [models.Index(fields=["status", "created_at"], name="posts_report_open_idx")]
