"""Spec 15 §12.4: one register of every inbound webhook. No payload is stored (it holds personal data), only its hash."""
import uuid

from django.db import models


class WebhookReceipt(models.Model):
    class Status(models.TextChoices):
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=128)  # Svix ID, or a hash of event + reference + body
    event_type = models.CharField(max_length=64, blank=True)
    subject = models.CharField(max_length=64, blank=True)  # a Clerk user ID or a payment reference: what a retry needs
    body_sha256 = models.CharField(max_length=64, blank=True)
    signature_valid = models.BooleanField(default=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROCESSED)
    attempts = models.PositiveSmallIntegerField(default=1)
    error = models.CharField(max_length=255, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [models.UniqueConstraint(fields=["provider", "event_id"], name="integrations_one_receipt_per_event")]
        indexes = [models.Index(fields=["provider", "status", "received_at"], name="integrations_receipt_idx")]
