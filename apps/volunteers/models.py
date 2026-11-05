"""
Volunteering — spec 18 §9, §11.2 (FR-047–FR-050). Applying for a campaign shows support for that candidate: political-opinion
data, seen only by the person and the campaign they applied to.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.fields import StateField, choice_check, state_check


class Role(models.TextChoices):
    CANVASSING = "canvassing", "Canvassing"
    EVENTS = "events", "Events"
    SOCIAL_MEDIA = "social_media", "Social media"
    LOGISTICS = "logistics", "Logistics"
    DATA_AND_TECH = "data_and_tech", "Data and tech"
    OUTREACH_YOUTH = "outreach_youth", "Youth outreach"
    OUTREACH_WOMEN = "outreach_women", "Women's outreach"
    OUTREACH_DISABILITY = "outreach_disability", "Disability outreach"
    OTHER = "other", "Other"


class Availability(models.TextChoices):
    WEEKDAYS = "weekdays", "Weekdays"
    WEEKDAY_EVENINGS = "weekday_evenings", "Weekday evenings"
    WEEKENDS = "weekends", "Weekends"


class VolunteerOpening(models.Model):
    """A campaign's volunteer sign-up (spec 18 §9.1). One per candidate, off by default."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.OneToOneField("candidates.Candidate", on_delete=models.PROTECT, related_name="volunteer_opening")
    open = models.BooleanField(default=False)
    intro = models.CharField(max_length=1000, blank=True)
    roles = models.JSONField(default=list, blank=True)
    states = models.JSONField(default=list, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(name="volunteers_open_has_time", condition=Q(open=False) | Q(opened_at__isnull=False))]


class AppStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    WITHDRAWN = "withdrawn", "Withdrawn"
    ENDED = "ended", "Ended"


OPEN_STATUSES = (AppStatus.PENDING, AppStatus.ACCEPTED)


class VolunteerApplication(models.Model):
    """One person's offer to volunteer for one campaign (spec 18 §9.2). Only what's needed to coordinate (FR-048)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey("candidates.Candidate", on_delete=models.PROTECT, related_name="volunteer_applications")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="volunteer_applications")
    roles = models.JSONField(default=list)
    state = StateField()
    lga_text = models.CharField(max_length=100, blank=True)
    geo_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    availability = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=300, blank=True)
    share_phone = models.BooleanField(default=False)
    status = models.CharField(max_length=10, choices=AppStatus.choices, default=AppStatus.PENDING)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decline_reason = models.CharField(max_length=200, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["candidate", "status", "created_at"], name="volunteers_queue_idx")]
        constraints = [
            models.UniqueConstraint(fields=["candidate", "user"], condition=Q(status__in=["pending", "accepted"]),
                                    name="volunteers_one_open_application"),
            choice_check("status", AppStatus.values, "volunteers_status_valid"),
            state_check("state", "volunteers_state_valid", allow_blank=False),
            models.CheckConstraint(name="volunteers_decided_shape",
                                   condition=~Q(status__in=["accepted", "declined"]) | Q(decided_at__isnull=False)),
            models.CheckConstraint(name="volunteers_roles_not_empty", condition=~Q(roles=[])),
        ]


class VolunteerUpdate(models.Model):
    """A campaign's message to its accepted volunteers (spec 18 §9.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    candidate = models.ForeignKey("candidates.Candidate", on_delete=models.PROTECT, related_name="volunteer_updates")
    role = models.CharField(max_length=20, blank=True)  # blank = every accepted volunteer
    text = models.CharField(max_length=500)
    sent_to = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(name="volunteers_update_role_valid", condition=Q(role="") | Q(role__in=Role.values))]


class VolunteerChangeKind(models.TextChoices):
    APPLIED = "applied", "Applied"
    ACCEPTED = "accepted", "Accepted"
    DECLINED = "declined", "Declined"
    WITHDRAWN = "withdrawn", "Withdrawn"
    ENDED = "ended", "Ended"
    PHONE_VIEWED = "phone_viewed", "Contact viewed"


class VolunteerChange(models.Model):
    """Append-only history of an application (spec 18 §11.2)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(VolunteerApplication, on_delete=models.CASCADE, related_name="changes")
    kind = models.CharField(max_length=14, choices=VolunteerChangeKind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [choice_check("kind", VolunteerChangeKind.values, "volunteers_change_kind_valid")]
