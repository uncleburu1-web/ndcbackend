from apps.privacy.exporting import pick

DESCRIPTION = "Candidates you follow and whether their notifications are on."


def export_for(user) -> dict:
    from .models import CandidateFollow

    rows = CandidateFollow.objects.filter(user=user).select_related("candidate").order_by("created_at")
    return {"follows": [pick(f, [("candidate", lambda f: f.candidate.full_name), ("candidate_slug", lambda f: f.candidate.slug), "notify",
                                 ("followed_at", lambda f: f.created_at)]) for f in rows]}
