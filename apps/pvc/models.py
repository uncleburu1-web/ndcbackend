"""PVC verification — spec 06 §5.1. The VIN is encrypted at rest; only the last 4 characters are ever shown."""
import uuid

from django.conf import settings
from django.db import models

from apps.candidates.reference import State


class PvcStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SUCCESSFUL = "successful", "Successful"
    REJECTED = "rejected", "Rejected"


class PvcVerification(models.Model):
    """One current record per user. Withdrawn submissions are deleted outright (spec 06 §10)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pvc")
    vin_encrypted = models.BinaryField(blank=True, default=b"")
    vin_last4 = models.CharField(max_length=4, blank=True)
    surname = models.CharField(max_length=80)
    state = models.CharField(max_length=16, choices=State.choices)
    lga = models.CharField(max_length=120)
    polling_unit = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=12, choices=PvcStatus.choices, default=PvcStatus.PENDING, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    consent_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

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
