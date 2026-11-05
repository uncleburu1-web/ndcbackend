import uuid

from django.db import models


class TimeStampedModel(models.Model):
    """UUID primary key (non-enumerable) + created/updated timestamps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class JobRun(models.Model):
    """One run of a scheduled management command (spec 11 §5.2). No personal data in summary or error."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.CharField(max_length=64, db_index=True)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(null=True)
    summary = models.CharField(max_length=255, blank=True, default="")
    error = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["job", "-started_at"])]

    def __str__(self):
        return f"{self.job} @ {self.started_at:%Y-%m-%d %H:%M}"


class OutboxJob(models.Model):
    """Spec 15 §9.3: work that must happen after a commit and must not be lost. Written in the caller's transaction.
    `payload` holds IDs only — never personal data, tokens or keys."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=32)
    payload = models.JSONField(default=dict)
    dedupe_key = models.CharField(max_length=128, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    last_error = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["next_attempt_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="core_outbox_due_idx"),
                   # spec 16 F8: the prune path
                   models.Index(fields=["finished_at"], name="core_outbox_finished_idx", condition=models.Q(status__in=["done", "failed"]))]
        constraints = [models.UniqueConstraint(fields=["dedupe_key"], condition=models.Q(status__in=["pending", "running"]) & ~models.Q(dedupe_key=""),
                                               name="core_outbox_one_pending_per_key"),
                       # spec 16 §6.3
                       models.CheckConstraint(condition=~models.Q(status__in=["done", "failed"]) | models.Q(finished_at__isnull=False),
                                              name="core_outboxjob_finished_shape"),
                       models.CheckConstraint(condition=models.Q(status__in=["pending", "running", "done", "failed"]),
                                              name="core_outbox_status_valid")]
