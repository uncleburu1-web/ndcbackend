"""
Candidate posts — spec 09 §5. Linked campaign accounts publish directly: there is no Pending status, ever.
"Visible" = published post of a published, verified candidate (CandidatePost.objects.visible()).
"""
import uuid

from django.conf import settings
from django.db import models

from apps.core.fields import choice_check
from apps.core.i18n import language_check, language_field


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
    """An announcement or news post published by a candidate's linked campaign accounts (spec 09)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey("candidates.Candidate", on_delete=models.PROTECT, related_name="posts")
    kind = models.CharField(max_length=12, choices=PostKind.choices)
    title = models.CharField(max_length=150)
    body = models.TextField(max_length=10_000)
    language = language_field()
    image = models.CharField(max_length=255, blank=True)
    image_thumb = models.CharField(max_length=255, blank=True)
    image_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # spec 15 §7.4
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
    removed_rule = models.CharField(max_length=8, blank=True)  # spec 13 §5.4
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PostQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at"]
        indexes = [
            models.Index(fields=["candidate", "status", "-published_at"], name="posts_candidate_idx"),
            models.Index(fields=["status", "-published_at"], name="posts_feed_idx"),
        ]
        constraints = [language_check("posts_language_valid", "language"),   # spec 16 §6.3
            models.CheckConstraint(condition=~models.Q(status="removed") | models.Q(removed_at__isnull=False), name="posts_post_removed_shape"),
            models.CheckConstraint(condition=~models.Q(status="deleted") | models.Q(deleted_at__isnull=False), name="posts_post_deleted_shape"),
            choice_check("status", PostStatus.values, "posts_status_valid"),
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
