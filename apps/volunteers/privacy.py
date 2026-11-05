"""Spec 18 §17.4: your volunteer applications, decisions and the updates campaigns sent you."""
from apps.privacy.exporting import pick

DESCRIPTION = "Your volunteer applications (roles, area, availability, note), the campaigns' decisions and their updates to you."


def export_for(user) -> dict:
    from .models import AppStatus, VolunteerApplication, VolunteerUpdate

    apps = VolunteerApplication.objects.filter(user=user).select_related("candidate").order_by("created_at")
    accepted = [a.candidate_id for a in apps if a.status == AppStatus.ACCEPTED]
    updates = VolunteerUpdate.objects.filter(candidate_id__in=accepted).select_related("candidate").order_by("created_at")
    return {"applications": [pick(a, [("campaign", lambda a: a.candidate.full_name), "roles", "state", "lga_text", "availability", "note",
                                      "share_phone", "status", "decline_reason", "created_at", "decided_at", "ended_at"]) for a in apps],
            "updates": [pick(u, [("campaign", lambda u: u.candidate.full_name), "role", "text", "created_at"]) for u in updates]}
