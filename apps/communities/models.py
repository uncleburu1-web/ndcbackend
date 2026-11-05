"""
Communities — spec 17 §11. National and state communities exist from the start; LGA, ward (registration area) and
polling-unit communities are created from `apps/geo` by `sync_communities`. Who belongs where is political-opinion
and location data: never listed to anyone (rule 2).
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.fields import StateField, choice_check, state_check


class Level(models.TextChoices):
    NATIONAL = "national", "National"
    STATE = "state", "State"
    LGA = "lga", "LGA"
    WARD = "ward", "Ward (registration area)"
    POLLING_UNIT = "polling_unit", "Polling unit"


LEVEL_ORDER = [Level.NATIONAL, Level.STATE, Level.LGA, Level.WARD, Level.POLLING_UNIT]
LOWER_LEVELS = [Level.LGA, Level.WARD, Level.POLLING_UNIT]


class ReadScope(models.TextChoices):
    SIGNED_IN = "signed_in", "Signed-in users"
    MEMBERS = "members", "Members only"


class Community(models.Model):
    """One community: national, one per state, or one per active geo LGA, ward or polling unit (spec 17 §4)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    level = models.CharField(max_length=12, choices=Level.choices)
    state = StateField()
    geo_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_ward = models.ForeignKey("geo.Ward", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_polling_unit = models.ForeignKey("geo.PollingUnit", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    name = models.CharField(max_length=240)
    path = models.CharField(max_length=200, unique=True)
    posting_open = models.BooleanField(default=True)
    read_scope = models.CharField(max_length=10, choices=ReadScope.choices, default=ReadScope.SIGNED_IN)
    ndc_members_only = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    member_count = models.PositiveIntegerField(default=0)
    post_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["path"]
        indexes = [models.Index(fields=["parent", "active"], name="communities_parent_idx")]
        constraints = [
            choice_check("level", Level.values, "communities_level_valid"),
            choice_check("read_scope", ReadScope.values, "communities_read_scope_valid"),
            state_check("state", "communities_state_valid"),
            models.CheckConstraint(name="communities_community_state_shape",
                                   condition=(Q(level="national") & Q(state="")) | (~Q(level="national") & ~Q(state=""))),
            models.CheckConstraint(name="communities_community_geo_shape", condition=(
                (Q(level__in=["national", "state"]) & Q(geo_lga__isnull=True, geo_ward__isnull=True, geo_polling_unit__isnull=True))
                | (Q(level="lga") & Q(geo_lga__isnull=False, geo_ward__isnull=True, geo_polling_unit__isnull=True))
                | (Q(level="ward") & Q(geo_lga__isnull=True, geo_ward__isnull=False, geo_polling_unit__isnull=True))
                | (Q(level="polling_unit") & Q(geo_lga__isnull=True, geo_ward__isnull=True, geo_polling_unit__isnull=False)))),
            models.CheckConstraint(name="communities_community_parent_shape",
                                   condition=(Q(level="national") & Q(parent__isnull=True)) | (~Q(level="national") & Q(parent__isnull=False))),
            models.UniqueConstraint(fields=["level"], condition=Q(level="national"), name="communities_one_national"),
            models.UniqueConstraint(fields=["state"], condition=Q(level="state"), name="communities_one_per_state"),
            models.UniqueConstraint(fields=["geo_lga"], condition=Q(geo_lga__isnull=False), name="communities_one_per_lga"),
            models.UniqueConstraint(fields=["geo_ward"], condition=Q(geo_ward__isnull=False), name="communities_one_per_ward"),
            models.UniqueConstraint(fields=["geo_polling_unit"], condition=Q(geo_polling_unit__isnull=False), name="communities_one_per_pu"),
        ]

    def __str__(self):
        return self.path


class How(models.TextChoices):
    VERIFIED = "verified_registration", "From your verified PVC"
    CHOSEN = "chosen", "You chose it"


class MembershipStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    ENDED = "ended", "Ended"


class EndReason(models.TextChoices):
    LEFT = "left", "You left"
    MOVED = "moved", "Your verified polling unit changed"
    PVC_ENDED = "pvc_ended", "Your PVC check ended"
    CONSENT_WITHDRAWN = "consent_withdrawn", "You withdrew consent"
    COMMUNITY_CLOSED = "community_closed", "The community closed"


class CommunityMembership(models.Model):
    """One person's membership of one community; private like follows (spec 17 §5, §11.2). Never listed to anyone."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="community_memberships")
    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="memberships")
    level = models.CharField(max_length=12, choices=Level.choices)
    how = models.CharField(max_length=24, choices=How.choices)
    status = models.CharField(max_length=8, choices=MembershipStatus.choices, default=MembershipStatus.ACTIVE)
    end_reason = models.CharField(max_length=20, choices=EndReason.choices, blank=True)
    notify_new_posts = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            choice_check("level", Level.values, "communities_membership_level_valid"),
            choice_check("how", How.values, "communities_membership_how_valid"),
            choice_check("status", MembershipStatus.values, "communities_membership_status_valid"),
            models.CheckConstraint(name="communities_membership_end_shape",
                                   condition=(Q(status="active") & Q(left_at__isnull=True, end_reason=""))
                                   | (Q(status="ended") & Q(left_at__isnull=False) & ~Q(end_reason=""))),
            models.CheckConstraint(name="communities_membership_chosen_state_only", condition=~Q(how="chosen") | Q(level="state")),
            models.CheckConstraint(name="communities_membership_no_national", condition=~Q(level="national")),
            models.CheckConstraint(name="communities_membership_bell_lower_only",
                                   condition=Q(notify_new_posts=False) | Q(level__in=["lga", "ward", "polling_unit"])),
            models.UniqueConstraint(fields=["user", "community"], condition=Q(status="active"), name="communities_one_active_per_community"),
            models.UniqueConstraint(fields=["user", "level"], condition=Q(status="active"), name="communities_one_active_per_level"),
        ]
        indexes = [models.Index(fields=["community", "status"], name="communities_member_idx")]


class CommunitySettings(models.Model):
    """A person's community settings: levels they left (not re-added by placement) and the 30-day state rule (spec 17 §11.3)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True, related_name="community_settings")
    left_state = models.BooleanField(default=False)
    left_lga = models.BooleanField(default=False)
    left_ward = models.BooleanField(default=False)
    left_polling_unit = models.BooleanField(default=False)
    state_changed_at = models.DateTimeField(null=True, blank=True)
    consent_version = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class ItemStatus(models.TextChoices):
    PUBLISHED = "published", "Published"
    HIDDEN = "hidden", "Hidden while moderators review it"
    REMOVED = "removed", "Removed"
    DELETED = "deleted", "Deleted"


def _item_constraints(prefix):
    return [
        choice_check("status", ItemStatus.values, f"communities_{prefix}_status_valid"),
        models.CheckConstraint(name=f"communities_{prefix}_hidden_shape", condition=~Q(status="hidden") | Q(hidden_at__isnull=False)),
        models.CheckConstraint(name=f"communities_{prefix}_removed_shape",
                               condition=~Q(status="removed") | (Q(removed_at__isnull=False) & ~Q(removed_rule=""))),
        models.CheckConstraint(name=f"communities_{prefix}_deleted_shape", condition=~Q(status="deleted") | Q(deleted_at__isnull=False)),
    ]


class CommunityPost(models.Model):
    """A text post in a community (spec 17 §7.1, §11.4). Plain text; shown with the author's short name."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    community = models.ForeignKey(Community, on_delete=models.PROTECT, related_name="posts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="community_posts")
    campaign_candidate = models.ForeignKey("candidates.Candidate", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    body = models.TextField(max_length=2000)
    status = models.CharField(max_length=10, choices=ItemStatus.choices, default=ItemStatus.PUBLISHED)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_rule = models.CharField(max_length=8, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    body_erased_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    comment_count = models.PositiveIntegerField(default=0)
    reaction_count = models.PositiveIntegerField(default=0)
    body_hash = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["community", "status", "-created_at"], name="communities_feed_idx"),
            models.Index(fields=["author", "created_at"], name="communities_post_author_idx"),
            models.Index(fields=["body_hash", "author", "created_at"], name="communities_post_dupe_idx"),
        ]
        constraints = _item_constraints("post")


class CommunityComment(models.Model):
    """A comment on a community post; one level, no replies to comments (spec 17 §7.2)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(CommunityPost, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="community_comments")
    campaign_candidate = models.ForeignKey("candidates.Candidate", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    body = models.TextField(max_length=1000)
    status = models.CharField(max_length=10, choices=ItemStatus.choices, default=ItemStatus.PUBLISHED)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    removed_rule = models.CharField(max_length=8, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    body_erased_at = models.DateTimeField(null=True, blank=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["post", "status", "created_at"], name="communities_comment_idx"),
                   models.Index(fields=["author", "created_at"], name="communities_comment_author_idx")]
        constraints = _item_constraints("comment")


class ReactionKind(models.TextChoices):
    LIKE = "like", "Like"


class CommunityReaction(models.Model):
    """One person's Like on a post (spec 17 §7.3). Private: only the count is ever shown."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(CommunityPost, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="community_reactions")
    kind = models.CharField(max_length=8, choices=ReactionKind.choices, default=ReactionKind.LIKE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            choice_check("kind", ReactionKind.values, "communities_reaction_kind_valid"),
            models.UniqueConstraint(fields=["post", "user", "kind"], name="communities_one_reaction"),
        ]


class EventKind(models.TextChoices):
    CREATED = "created"
    EDITED = "edited"
    HIDDEN = "hidden"
    UNHIDDEN = "unhidden"
    REMOVED = "removed"
    RESTORED = "restored"
    DELETED = "deleted"
    BODY_ERASED = "body_erased"
    AUTHOR_DETACHED = "author_detached"


class CommunityEvent(models.Model):
    """Append-only history of community posts and comments (spec 17 §11.6). The database refuses edits (spec 16)."""

    Kind = EventKind

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(CommunityPost, on_delete=models.CASCADE, related_name="events")
    comment = models.ForeignKey(CommunityComment, null=True, blank=True, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=16, choices=EventKind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    previous_body = models.TextField(blank=True)  # edited only; cleared with the post or comment's prune (spec 17 §17.3)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [choice_check("kind", EventKind.values, "communities_event_kind_valid"),
                       models.CheckConstraint(name="communities_event_body_shape", condition=Q(kind="edited") | Q(previous_body=""))]
