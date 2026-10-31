"""Candidate following — spec 04 §5. Vote = Follow; never an official ballot."""
import uuid

from django.conf import settings
from django.db import models


class CandidateFollow(models.Model):
    """
    One row per (user, candidate). Unfollowing deletes the row: no history is kept,
    because who follows whom is political-opinion data (NDPA 2023, spec 04 §10).
    Follows are never written to the audit log.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="candidate_follows")
    # Candidates are never hard-deleted (spec 03 §6.2); PROTECT makes that explicit.
    candidate = models.ForeignKey("candidates.Candidate", on_delete=models.PROTECT, related_name="follows")
    notify = models.BooleanField(default=False)  # opt-in (§3 rule 6)
    notify_changed_at = models.DateTimeField(null=True, blank=True)  # unit 05 skips posts published before this (§6.3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "candidate"], name="follows_one_per_user_candidate")]
        indexes = [
            models.Index(fields=["candidate", "notify"], name="follows_candidate_notify_idx"),
            models.Index(fields=["user", "-created_at"], name="follows_user_recent_idx"),
        ]

    def __str__(self):
        return f"follow {self.pk}"
