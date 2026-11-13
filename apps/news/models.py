"""
News and official announcements — spec 07 §5. One table for both sources so the feed, article pages and
reports share one model. Nothing a member submits is public until a reviewer approves it.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.fields import StateField, choice_check, state_check
from apps.core.i18n import language_check, language_field


class Source(models.TextChoices):
    OFFICIAL = "official", "Official"
    COMMUNITY = "community", "Community"


class ArticleStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCESSFUL = "successful", "Successful"
    REJECTED = "rejected", "Rejected"
    WITHDRAWN = "withdrawn", "Withdrawn"
    REMOVED = "removed", "Removed"


class Category(models.TextChoices):
    """FR-024 — official announcements only."""

    ELECTION_UPDATES = "election_updates", "Official Election Updates"
    PARTY = "party", "Party Announcements"
    CAMPAIGN = "campaign", "Campaign Announcements"
    COMMUNITY = "community", "Community Announcements"
    ELECTION_NEWS = "election_news", "General Election News"


class ArticleQuerySet(models.QuerySet):
    def public(self):
        return self.filter(status=ArticleStatus.SUCCESSFUL)


class Article(models.Model):
    """An official announcement or a member's community news story (spec 07)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=10, choices=Source.choices)
    status = models.CharField(max_length=12, choices=ArticleStatus.choices, default=ArticleStatus.PENDING)
    category = models.CharField(max_length=20, choices=Category.choices, blank=True)
    title = models.CharField(max_length=150)
    body = models.TextField()
    # spec 21 §9: the language it is written in ("und" = not sure); shown with lang=
    language = language_field()
    state = StateField()
    source_name = models.CharField(max_length=120, blank=True)
    source_url = models.URLField(max_length=500, blank=True)
    image = models.CharField(max_length=255, blank=True)
    image_thumb = models.CharField(max_length=255, blank=True)
    image_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")  # spec 15 §7.4
    image_width = models.PositiveIntegerField(null=True, blank=True)
    image_height = models.PositiveIntegerField(null=True, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="news_articles")
    show_author_name = models.BooleanField(default=False)
    declarations = models.JSONField(default=dict, blank=True)
    pinned_at = models.DateTimeField(null=True, blank=True)
    # spec 17 §9: an official announcement shown pinned in this community and the levels below it; null = everyone
    audience_community = models.ForeignKey("communities.Community", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    important = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=500, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_reason = models.CharField(max_length=500, blank=True)
    removed_rule = models.CharField(max_length=8, blank=True)  # spec 13 §5.4: "R3", or "LEGAL" for legal-notice removals
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ArticleQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at", "-created_at"]
        constraints = [language_check("news_article_language_valid", "language"), 
            models.CheckConstraint(
                name="news_official_shape",
                condition=Q(source="community") | (~Q(category="") & ~Q(status="pending")),
            ),
            models.CheckConstraint(
                name="news_community_shape",
                condition=Q(source="official") | (Q(category="") & Q(pinned_at__isnull=True)),
            ),
            # spec 16 §6.3
            models.CheckConstraint(condition=~Q(status="successful") | Q(published_at__isnull=False), name="news_article_published_shape"),
            models.CheckConstraint(condition=Q(audience_community__isnull=True) | Q(source="official"), name="news_article_audience_shape"),
            models.CheckConstraint(condition=~Q(status="removed") | Q(removed_at__isnull=False), name="news_article_removed_shape"),
            choice_check("status", ArticleStatus.values, "news_status_valid"),
            state_check("state", "news_state_valid"),
        ]
        indexes = [
            models.Index(fields=["status", "-published_at"], name="news_feed_idx"),
            models.Index(fields=["source", "status", "-published_at"], name="news_source_idx"),
            models.Index(fields=["state", "status", "-published_at"], name="news_state_idx"),
            models.Index(fields=["author", "-created_at"], name="news_author_idx"),
            models.Index(fields=["submitted_at"], name="news_queue_idx", condition=Q(status="pending")),
        ]

    def __str__(self):
        return f"{self.source}:{self.title[:40]}"


class TranslationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"


class ArticleTranslation(models.Model):
    """Spec 21 §10: an official announcement in another language, entered by a person (never machine-made) and reviewed
    by a second person before it is published. The image and source link are the original's."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="translations")
    language = models.CharField(max_length=3)
    title = models.CharField(max_length=150)
    body = models.TextField()
    status = models.CharField(max_length=9, choices=TranslationStatus.choices, default=TranslationStatus.DRAFT)
    translated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    original_updated_at = models.DateTimeField()  # the original's last edit when this version was written or reviewed
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["language"]
        constraints = [
            models.UniqueConstraint(fields=["article", "language"], name="news_translation_one_per_language"),
            models.CheckConstraint(condition=Q(language__in=["ha", "ig", "yo", "pcm", "en"]), name="news_translation_language_valid"),
            choice_check("status", TranslationStatus.values, "news_translation_status_valid"),
            models.CheckConstraint(condition=~Q(status="published") | (Q(published_at__isnull=False) & Q(reviewed_by__isnull=False)),
                                   name="news_translation_published_shape"),
            models.CheckConstraint(condition=Q(reviewed_by__isnull=True) | ~Q(reviewed_by=models.F("translated_by")),
                                   name="news_translation_second_person"),
        ]

    def __str__(self):
        return f"{self.article_id}:{self.language}"


class ArticleEvent(models.Model):
    """Append-only history. Old text isn't kept (spec 07 §10)."""

    class Kind(models.TextChoices):
        SUBMITTED = "submitted"
        EDITED = "edited"
        RESUBMITTED = "resubmitted"
        APPROVED = "approved"
        REJECTED = "rejected"
        WITHDRAWN = "withdrawn"
        REMOVED = "removed"
        RESTORED = "restored"
        PUBLISHED = "published"
        UNPUBLISHED = "unpublished"
        PINNED = "pinned"
        UNPINNED = "unpinned"
        BYLINE_CHANGED = "byline_changed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    article = models.ForeignKey(Article, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=20, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("ArticleEvent is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("ArticleEvent is append-only.")
