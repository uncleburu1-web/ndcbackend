"""
Events — spec 18 §4, §6, §11.1. Campaign events (accounts linked to a published candidate) and party events (system admins).
Who registered is political-opinion data: never listed beyond the organiser's check-in window (rule 7).
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.fields import StateField, choice_check, state_check
from apps.core.i18n import language_check, language_field


class Organiser(models.TextChoices):
    CAMPAIGN = "campaign", "Campaign"
    PARTY = "party", "NDC"


class EventType(models.TextChoices):
    RALLY = "rally", "Rally"
    CANVASS = "canvass", "Canvassing drive"
    TOWN_HALL = "town_hall", "Town hall"
    MEETING = "meeting", "Meeting"
    TRAINING = "training", "Training"
    OTHER = "other", "Other"


CAMPAIGNING_TYPES = (EventType.RALLY, EventType.CANVASS)  # rule 2: inside the office's campaign period


class Mode(models.TextChoices):
    IN_PERSON = "in_person", "In person"
    ONLINE = "online", "Online"
    HYBRID = "hybrid", "In person and online"


class VenueType(models.TextChoices):
    """Rule 3: no place of worship, police station or public office (Electoral Act 2026 s. 96)."""
    OPEN_GROUND = "open_ground", "Open ground or field"
    HALL = "hall", "Hall or event centre"
    HOTEL = "hotel", "Hotel"
    SCHOOL = "school", "School (outside school hours)"
    MARKET = "market", "Market"
    STREET = "street", "Street or neighbourhood"
    PRIVATE_PREMISES = "private_premises", "Private premises"
    ONLINE = "online", "Online only"


class AddressVisibility(models.TextChoices):
    PUBLIC = "public", "Everyone"
    REGISTERED = "registered", "Registered people only"


class Registration(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"
    NONE = "none", "No sign-up (information only)"


class EventStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    HIDDEN = "hidden", "Hidden while deciding"
    CANCELLED = "cancelled", "Cancelled"
    REMOVED = "removed", "Removed"


class MaterialChange(models.TextChoices):
    TIME = "time", "time changed"
    VENUE = "venue", "venue changed"
    MODE = "mode", "changed to online or in person"
    LINK = "link", "online link changed"
    POSTPONED = "postponed", "postponed"


class Event(models.Model):
    """One campaign or party event (spec 18 §4.2). Published events are cancelled, never deleted, until retention."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organiser_kind = models.CharField(max_length=10, choices=Organiser.choices)
    candidate = models.ForeignKey("candidates.Candidate", null=True, blank=True, on_delete=models.PROTECT, related_name="events")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    type = models.CharField(max_length=12, choices=EventType.choices)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.IN_PERSON)
    title = models.CharField(max_length=120)
    description = models.TextField(max_length=4000)
    language = language_field()
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    venue_name = models.CharField(max_length=200, blank=True)
    venue_address = models.CharField(max_length=200, blank=True)
    venue_type = models.CharField(max_length=20, choices=VenueType.choices, blank=True)
    address_visibility = models.CharField(max_length=10, choices=AddressVisibility.choices, default=AddressVisibility.PUBLIC)
    state = StateField(blank=True)  # blank = nationwide (online)
    lga_text = models.CharField(max_length=100, blank=True)
    geo_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    online_url = models.URLField(max_length=500, blank=True)
    accessibility_notes = models.CharField(max_length=300, blank=True)
    capacity = models.PositiveIntegerField(null=True, blank=True)
    registration = models.CharField(max_length=8, choices=Registration.choices, default=Registration.OPEN)
    registration_closes_at = models.DateTimeField(null=True, blank=True)
    members_only = models.BooleanField(default=False)
    cover_image = models.CharField(max_length=255, blank=True)
    cover_image_thumb = models.CharField(max_length=255, blank=True)
    cover_image_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=10, choices=EventStatus.choices, default=EventStatus.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=300, blank=True)
    hidden_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_rule = models.CharField(max_length=10, blank=True)
    declarations = models.JSONField(default=dict, blank=True)
    registered_count = models.PositiveIntegerField(default=0)
    waitlist_count = models.PositiveIntegerField(default=0)
    checked_in_count = models.PositiveIntegerField(default=0)
    material_change_at = models.DateTimeField(null=True, blank=True)
    material_change_kind = models.CharField(max_length=10, choices=MaterialChange.choices, blank=True)
    flagged_blackout_at = models.DateTimeField(null=True, blank=True)  # events_tick: a blackout window now covers the start
    reminder_day_sent_at = models.DateTimeField(null=True, blank=True)
    reminder_hour_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["starts_at", "id"]
        indexes = [models.Index(fields=["status", "starts_at"], name="events_status_start_idx"),
                   models.Index(fields=["state", "starts_at"], name="events_state_start_idx"),
                   models.Index(fields=["candidate", "starts_at"], name="events_candidate_start_idx"),
                   models.Index(fields=["geo_lga", "starts_at"], name="events_lga_start_idx")]
        constraints = [language_check("events_language_valid", "language"), 
            choice_check("organiser_kind", Organiser.values, "events_organiser_valid"),
            choice_check("type", EventType.values, "events_type_valid"),
            choice_check("mode", Mode.values, "events_mode_valid"),
            choice_check("status", EventStatus.values, "events_status_valid"),
            choice_check("registration", Registration.values, "events_registration_valid"),
            choice_check("address_visibility", AddressVisibility.values, "events_address_visibility_valid"),
            models.CheckConstraint(name="events_venue_type_valid", condition=Q(venue_type="") | Q(venue_type__in=VenueType.values)),
            state_check("state", "events_state_valid"),
            models.CheckConstraint(name="events_ends_after_start", condition=Q(ends_at__gt=models.F("starts_at"))),
            models.CheckConstraint(name="events_organiser_shape",
                                   condition=Q(organiser_kind="campaign", candidate__isnull=False) | Q(organiser_kind="party", candidate__isnull=True)),
            models.CheckConstraint(name="events_in_person_has_venue",
                                   condition=Q(mode="online") | (~Q(venue_name="") & ~Q(venue_address="") & ~Q(venue_type=""))),
            models.CheckConstraint(name="events_online_has_link", condition=Q(mode="in_person") | ~Q(online_url="")),
            models.CheckConstraint(name="events_capacity_positive", condition=Q(capacity__isnull=True) | Q(capacity__gte=1)),
            models.CheckConstraint(name="events_members_only_party", condition=Q(members_only=False) | Q(organiser_kind="party")),
            models.CheckConstraint(name="events_published_has_time", condition=Q(status="draft") | Q(published_at__isnull=False)),
            models.CheckConstraint(name="events_cancelled_shape",
                                   condition=~Q(status="cancelled") | (Q(cancelled_at__isnull=False) & ~Q(cancel_reason=""))),
            models.CheckConstraint(name="events_removed_shape",
                                   condition=~Q(status="removed") | (Q(removed_at__isnull=False) & ~Q(removed_rule=""))),
            models.CheckConstraint(name="events_hidden_shape", condition=~Q(status="hidden") | Q(hidden_at__isnull=False)),
        ]

    def __str__(self):
        return self.title

    @property
    def organiser_label(self) -> str:
        return "NDC" if self.organiser_kind == Organiser.PARTY else f"{self.candidate.full_name}'s campaign"


class RegistrationStatus(models.TextChoices):
    REGISTERED = "registered", "Registered"
    WAITLISTED = "waitlisted", "Waitlisted"
    CANCELLED = "cancelled", "Cancelled"


class EventRegistration(models.Model):
    """A person's place at an event (spec 18 §6). Seen by the person; by the organiser only as §6.4 says."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="registrations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_registrations")
    status = models.CharField(max_length=10, choices=RegistrationStatus.choices)
    ticket_code = models.CharField(max_length=6)
    waitlist_position = models.PositiveIntegerField(null=True, blank=True)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    cancelled_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(fields=["event", "user"], name="events_one_registration_per_user"),
            models.UniqueConstraint(fields=["event", "ticket_code"], name="events_ticket_unique"),
            choice_check("status", RegistrationStatus.values, "events_registration_status_valid"),
            models.CheckConstraint(name="events_registration_shape",
                                   condition=(Q(status="waitlisted", waitlist_position__isnull=False)
                                              | (~Q(status="waitlisted") & Q(waitlist_position__isnull=True)))
                                   & (Q(checked_in_at__isnull=True) | Q(status="registered"))),
        ]
        indexes = [models.Index(fields=["event", "status", "waitlist_position"], name="events_reg_queue_idx")]


class EventUpdate(models.Model):
    """An organiser's message to registrants (spec 18 §7): at most 3 per event."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="updates")
    text = models.CharField(max_length=500)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class ChangeKind(models.TextChoices):
    CREATED = "created", "Created"
    PUBLISHED = "published", "Published"
    EDITED = "edited", "Edited"
    MATERIAL_CHANGE = "material_change", "Material change"
    REGISTRATION_CLOSED = "registration_closed", "Registration closed"
    CANCELLED = "cancelled", "Cancelled"
    HIDDEN = "hidden", "Hidden while deciding"
    UNHIDDEN = "unhidden", "Shown again"
    REMOVED = "removed", "Removed"
    RESTORED = "restored", "Restored"
    CHECKIN_LIST_OPENED = "checkin_list_opened", "Check-in list opened"
    IMAGE_CHANGED = "image_changed", "Cover image changed"


class EventChange(models.Model):
    """Append-only history of an event (spec 18 §11.1). Never says which person registered."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="changes")
    kind = models.CharField(max_length=20, choices=ChangeKind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [choice_check("kind", ChangeKind.values, "events_change_kind_valid")]
