"""Spec 15 §7.4: one record per public image, wherever its bytes live (Cloudinary in production, local disk in
development). Content models point to it; the bytes never live in PostgreSQL."""
import uuid

from django.conf import settings
from django.db import models


class Purpose(models.TextChoices):
    CANDIDATE_PHOTO = "candidate_photo", "Candidate photo"
    POST_IMAGE = "post_image", "Candidate post image"
    NEWS_IMAGE = "news_image", "Community news image"
    ANNOUNCEMENT_IMAGE = "announcement_image", "Announcement image"


class MediaAsset(models.Model):
    """One public image asset (Cloudinary or local files) and its delivery state (spec 15 §7.4)."""

    class Provider(models.TextChoices):
        CLOUDINARY = "cloudinary", "Cloudinary"
        LOCAL = "local", "Local disk"

    class Delivery(models.TextChoices):
        PUBLIC = "public", "Public"
        HIDDEN = "hidden", "Hidden (removed by moderation)"
        DELETED = "deleted", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=12, choices=Provider.choices)
    public_id = models.CharField(max_length=200)  # Cloudinary public ID, or the local file stem
    version = models.CharField(max_length=20, blank=True)
    secure_url = models.CharField(max_length=500, blank=True)  # the stored original (Cloudinary) or the main local file
    format = models.CharField(max_length=10, blank=True)
    bytes = models.PositiveIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    local_files = models.JSONField(default=dict, blank=True)  # variant → storage name (local provider only)
    checksum = models.CharField(max_length=64, blank=True)  # local files only
    delivery = models.CharField(max_length=10, choices=Delivery.choices, default=Delivery.PUBLIC)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["provider", "public_id"], name="media_one_asset_per_public_id")]
        indexes = [models.Index(fields=["provider", "delivery"], name="media_asset_delivery_idx")]

    def __str__(self):
        return f"{self.provider}:{self.public_id}"
