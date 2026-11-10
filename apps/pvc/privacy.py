from apps.privacy.exporting import pick

DESCRIPTION = "Your PVC check. Your VIN is shown masked (ask the DPO for the full value)."


def _place(rec):
    from apps.membership.privacy import _place as place

    return place(rec)


def export_for(user) -> dict:
    from .models import PvcEvent, PvcVerification

    rec = PvcVerification.objects.filter(user=user).first()
    events = PvcEvent.objects.filter(user=user).order_by("created_at")
    return {"pvc": pick(rec, [("vin", lambda r: f"***************{r.vin_last4}"), "surname", "state", "lga", "polling_unit", "status",
                              "reviewed_at", "reason", "consent_at", "show_badge", "created_at", "geo_source",
                              ("chosen_place", lambda r: _place(r))]) if rec else None,
            "pvc_history": [pick(e, ["kind", "created_at"]) for e in events]}
