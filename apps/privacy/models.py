"""
Security and privacy records (spec 14 §5.1): consents, privacy requests, incidents and breaches,
access reviews, and the audit-chain anchor kept when old audit rows are pruned.
"""
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # as 13: no 0/O, 1/I/L


def new_reference(prefix: str) -> str:
    return f"{prefix}-" + "".join(secrets.choice(ALPHABET) for _ in range(6))


class Purpose(models.TextChoices):
    POLITICAL_FOLLOWING = "political_following", "Following candidates"
    MEMBERSHIP = "membership", "NDC membership application"
    PVC_CHECK = "pvc_check", "PVC check"
    VERIFIED_BADGE = "verified_badge", "Public verified-voter badge"


class ConsentRecord(models.Model):
    """A person's consent to one purpose at one version; withdrawn rather than deleted (spec 14)."""

    class Method(models.TextChoices):
        DIALOG = "dialog", "Consent dialog"
        FORM = "form", "Form declaration"
        BEFORE_RECORDS = "before_records", "Given before consent was recorded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="consents")
    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    version = models.PositiveSmallIntegerField()
    method = models.CharField(max_length=16, choices=Method.choices)
    granted_at = models.DateTimeField(auto_now_add=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawal_effect = models.CharField(max_length=200, blank=True)
    prompts_shown = models.PositiveSmallIntegerField(default=0)  # legacy follows confirmation (spec 14 §4.2, §11)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-granted_at"]
        constraints = [models.UniqueConstraint(fields=["user", "purpose"], condition=Q(withdrawn_at__isnull=True),
                                               name="privacy_one_active_consent")]


class DataRequest(models.Model):
    """A privacy request (access, correction, objection…) with its 30-day clock (spec 14)."""

    class Kind(models.TextChoices):
        ACCESS = "access", "Access to my data"
        RECTIFICATION = "rectification", "Correct my data"
        ERASURE = "erasure", "Erase specific data"
        OBJECTION = "objection", "Object to processing"
        RESTRICTION = "restriction", "Restrict processing"
        PORTABILITY = "portability", "Move my data elsewhere"
        WITHDRAW_CONSENT = "withdraw_consent", "Withdraw consent"
        OTHER = "other", "Something else"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        WAITING = "waiting", "Waiting for you"
        DONE = "done", "Done"
        REFUSED = "refused", "Refused"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="privacy_requests")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    details = models.CharField(max_length=2000, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    self_served = models.BooleanField(default=False)
    received_at = models.DateTimeField()
    due_at = models.DateTimeField()
    paused_at = models.DateTimeField(null=True, blank=True)
    paused_seconds = models.PositiveIntegerField(default=0)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_at"]
        indexes = [models.Index(fields=["status", "due_at"], name="privacy_request_queue_idx")]


class _AppendOnly(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError(f"{type(self).__name__} is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError(f"{type(self).__name__} is append-only.")


class DataRequestEvent(_AppendOnly):
    """Append-only history of a privacy request, including replies shown to the person."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(DataRequest, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=20)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=2000, blank=True)
    shown_to_person = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class Incident(models.Model):
    """An entry in the breach and incident register with its NDPC and ngCERT clocks (spec 14)."""

    class Risk(models.TextChoices):
        NONE = "none", "No risk to people"
        LOW = "low", "Some risk"
        HIGH = "high", "High risk"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CONTAINED = "contained", "Contained"
        CLOSED = "closed", "Closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=12, unique=True)
    title = models.CharField(max_length=200)
    description = models.CharField(max_length=4000, blank=True)
    detected_at = models.DateTimeField()
    aware_at = models.DateTimeField()
    systems = models.CharField(max_length=300, blank=True)
    data_categories = models.JSONField(default=list, blank=True)
    people_affected_estimate = models.PositiveIntegerField(null=True, blank=True)
    processor = models.CharField(max_length=40, blank=True)
    risk = models.CharField(max_length=8, choices=Risk.choices, default=Risk.NONE)
    is_personal_data_breach = models.BooleanField(default=False)
    is_cyber_incident = models.BooleanField(default=False)
    ndpc_notified_at = models.DateTimeField(null=True, blank=True)
    ndpc_reference = models.CharField(max_length=100, blank=True)
    ngcert_notified_at = models.DateTimeField(null=True, blank=True)
    ngcert_reference = models.CharField(max_length=100, blank=True)
    people_notified_at = models.DateTimeField(null=True, blank=True)
    notice_version = models.PositiveSmallIntegerField(default=0)  # bumps when risk/categories change; one notice per version
    notice_sent_version = models.PositiveSmallIntegerField(default=0)
    notice_text = models.CharField(max_length=1000, blank=True)  # the last security notice sent (shown on Your data)
    notice_sent_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    root_cause = models.CharField(max_length=2000, blank=True)
    lessons = models.CharField(max_length=2000, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-aware_at"]


class IncidentEvent(_AppendOnly):
    """Append-only timeline of an incident."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    incident = models.ForeignKey(Incident, on_delete=models.CASCADE, related_name="events")
    event = models.CharField(max_length=24)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=2000, blank=True)
    at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["at", "created_at"]


class AccessReview(models.Model):
    """A completed 90-day review of who holds admin roles (spec 14)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    completed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    completed_at = models.DateTimeField(auto_now_add=True)
    kept = models.PositiveIntegerField(default=0)
    revoked = models.PositiveIntegerField(default=0)
    notes = models.CharField(max_length=1000, blank=True)

    class Meta:
        ordering = ["-completed_at"]


class AuditAnchor(models.Model):
    """The last pruned audit row's hash: verification starts from here (spec 14 §4.8)."""

    seq = models.BigIntegerField(primary_key=True)
    hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-seq"]
