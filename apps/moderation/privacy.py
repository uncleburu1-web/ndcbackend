from apps.privacy.exporting import pick

DESCRIPTION = "Reports you made, and decisions, warnings, pauses and appeals about your own content. Never who reported you or who decided."


def export_for(user) -> dict:
    from . import services
    from .models import Appeal, Report, Restriction

    reports = Report.objects.filter(reporter=user).order_by("created_at")
    view = services.author_view(user)
    return {
        "reports_you_made": [pick(r, ["reference", "target_kind", "reason", "details", "status", "created_at", "resolved_at"]) for r in reports],
        "decisions_about_your_content": [{k: v for k, v in r.items() if k in ("target_kind", "title", "rule", "rule_title", "note_to_author",
                                                                               "decided_at", "decision", "source")} for r in view["removals"]],
        "restrictions": [pick(r, ["kind", "rule", "reason", "starts_at", "ends_at", "lifted_at"]) for r in Restriction.objects.filter(user=user)],
        "appeals": [pick(a, ["kind", "statement", "evidence_url", "status", "decided_at", "decision_reason", "created_at"])
                    for a in Appeal.objects.filter(appellant=user)],
    }
