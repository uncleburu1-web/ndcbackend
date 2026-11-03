from apps.privacy.exporting import pick

DESCRIPTION = "Roles you hold or held (admin roles and campaign links)."


def export_for(user) -> dict:
    rows = user.role_assignments.all().order_by("created_at")
    return {"roles": [pick(a, ["role", "scope_type", ("scope_id", lambda a: a.scope_id), ("granted_at", lambda a: a.created_at), "is_active",
                               "revoked_at"]) for a in rows]}
