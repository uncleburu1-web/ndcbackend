"""PVC verification — spec 06 §5.1. The VIN is encrypted at rest; only the last 4 characters are ever shown."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.core.fields import StateField, choice_check, state_check


class PvcStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCESSFUL = "successful", "Successful"
    REJECTED = "rejected", "Rejected"


class GeoSource(models.TextChoices):
    """Spec 19 §11.2. Community placement uses only `officer` and `matched` links (§8.1)."""
    MATCHED = "matched", "Exact match"
    CHOSEN = "chosen", "Chosen by the person"
    OFFICER = "officer", "Confirmed by an officer"
    REVIEW = "review", "Not listed (needs review)"


PLACEMENT_SOURCES = (GeoSource.OFFICER, GeoSource.MATCHED)


class PvcVerification(models.Model):
    """One current record per user. Withdrawn submissions are deleted outright (spec 06 §10)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pvc")
    vin_encrypted = models.BinaryField(blank=True, default=b"")
    vin_last4 = models.CharField(max_length=4, blank=True)
    surname = models.CharField(max_length=80)
    state = StateField(blank=False)
    lga = models.CharField(max_length=120)
    polling_unit = models.CharField(max_length=160, blank=True)
    # spec 16 §8.2: optional links to INEC's geography beside what the person typed (never overwritten)
    geo_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_ward = models.ForeignKey("geo.Ward", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_polling_unit = models.ForeignKey("geo.PollingUnit", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_source = models.CharField(max_length=8, choices=GeoSource.choices, blank=True, default="")  # spec 19 §7.3
    # spec 19 §8.1: on a verified check, the person's own choice waits here until an officer accepts it
    pending_geo_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    pending_geo_ward = models.ForeignKey("geo.Ward", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    pending_geo_polling_unit = models.ForeignKey("geo.PollingUnit", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_confirm_asked_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=PvcStatus.choices, default=PvcStatus.PENDING, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    consent_at = models.DateTimeField()
    show_badge = models.BooleanField(default=False)  # public "Verified voter" badge, opt-in (spec 08 §3.7)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["status", "state", "created_at"], name="pvc_queue_idx")]
        constraints = [  # spec 16 §6.3
            models.CheckConstraint(condition=Q(status="pending") | Q(reviewed_at__isnull=False), name="pvc_pvcverification_review_shape"),
            choice_check("status", PvcStatus.values, "pvc_status_valid"),
            state_check("state", "pvc_state_valid", allow_blank=False),
            choice_check("geo_source", [*GeoSource.values, ""], "pvc_geo_source_valid"),
        ]

    def __str__(self):
        return f"pvc {self.pk}"


class PvcEvent(models.Model):
    """Append-only history. Holds actions only — never the VIN."""

    class Kind(models.TextChoices):
        SUBMITTED = "submitted"
        DETAILS_CHANGED = "details_changed"
        WITHDRAWN = "withdrawn"
        VERIFIED = "verified"
        REJECTED = "rejected"
        VIN_REVEALED = "vin_revealed"
        VIN_PRUNED = "vin_pruned"
        BADGE_CHANGED = "badge_changed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pvc_events")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("PvcEvent is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("PvcEvent is append-only.")


class PvcSettings(models.Model):
    """Singleton: "Get your PVC" guidance that system admins can update without a release (spec 08 §5.4)."""

    guidance_note = models.CharField(max_length=1000, blank=True)
    collection_note = models.CharField(max_length=500, blank=True,
                                       default="Collection for the 2027 elections began on 9 October 2026 at INEC local government offices. "
                                               "Take your registration slip or details.")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="pvc_settings_single_row")]  # spec 16 §6.3

    @classmethod
    def current(cls) -> "PvcSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
