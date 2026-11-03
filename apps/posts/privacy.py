from apps.privacy.exporting import pick

DESCRIPTION = "Candidate posts you wrote or last edited as a campaign account."


def export_for(user) -> dict:
    from django.db.models import Q

    from .models import CandidatePost

    rows = CandidatePost.objects.filter(Q(created_by=user) | Q(updated_by=user)).select_related("candidate").order_by("created_at")
    return {"candidate_posts": [pick(p, ["id", ("candidate", lambda p: p.candidate.full_name), "kind", "title", "body", "status",
                                         "published_at", "edited_at", "deleted_at", "removed_at", "removed_reason", "removed_rule",
                                         ("you_wrote_it", lambda p: p.created_by_id == user.pk)]) for p in rows]}
