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
