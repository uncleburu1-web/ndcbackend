"""Candidate figures for the admin home (spec 11 §6.1)."""
from apps.access.services import is_system_admin
from apps.dashboard.cards import Card, Stat

from .models import Candidate, CandidateStatus


def admin_summary(user, *, now):
    if not is_system_admin(user):
        return []
    return [
        Card("candidates_unpublished", "Candidates not yet published",
             Candidate.objects.filter(status__in=[CandidateStatus.DRAFT, CandidateStatus.UNPUBLISHED]).count(), "/admin/candidates",
             info_only=True),
        Stat("candidates", "published", "Published candidates", Candidate.objects.filter(status=CandidateStatus.PUBLISHED).count()),
    ]
