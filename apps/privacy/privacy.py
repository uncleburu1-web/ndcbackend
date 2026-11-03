from apps.privacy.exporting import pick

DESCRIPTION = "Your consents (with versions and dates) and your privacy requests."


def export_for(user) -> dict:
    from .models import ConsentRecord, DataRequest

    return {
        "consents": [pick(c, ["purpose", "version", "method", "granted_at", "withdrawn_at", "withdrawal_effect"])
                     for c in ConsentRecord.objects.filter(user=user).order_by("granted_at")],
        "privacy_requests": [{**pick(r, ["reference", "kind", "details", "status", "received_at", "due_at", "closed_at", "outcome"]),
                              "updates": [pick(e, ["event", "note", "created_at"]) for e in r.events.filter(shown_to_person=True)]}
                             for r in DataRequest.objects.filter(user=user).order_by("received_at")],
    }
