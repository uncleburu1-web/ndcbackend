from apps.privacy.exporting import pick

DESCRIPTION = "Community news you submitted, with its review history (without the reviewers' identities)."


def export_for(user) -> dict:
    from .models import Article

    rows = Article.objects.filter(author=user, source="community").order_by("created_at")
    return {"news": [{**pick(a, ["id", "status", "title", "body", "state", "source_url", "image", "show_author_name", "declarations",
                                 "submitted_at", "published_at", "edited_at", "decided_at", "decision_reason", "removed_at", "removed_reason",
                                 "removed_rule", "created_at"]),
                      "history": [pick(e, ["event", "note", "created_at"]) for e in a.events.all().order_by("created_at")]} for a in rows]}
