from apps.privacy.exporting import pick

DESCRIPTION = "Your NDC membership application, its status and fee payments. Your NIN is shown masked (ask the DPO for the full value)."


def _place(rec):
    """Spec 19 §17.3: the chosen state, LGA, ward and polling unit, with INEC's codes."""
    from apps.geo.records import place

    p = place(rec.geo_lga, rec.geo_ward, rec.geo_polling_unit)
    return {"state": rec.state, **{k: ({"name": v["name"], "code": v["code"]} if v else None) for k, v in p.items()}}


def export_for(user) -> dict:
    from .models import Membership

    m = Membership.objects.filter(user=user).first()
    if m is None:
        return {"membership": None}
    return {"membership": {
        **pick(m, ["status", "full_name", "sex", "date_of_birth", "address", "state", "lga", "ward", "polling_unit",
                   ("nin", lambda m: f"*******{m.nin_last4}" if m.nin_last4 else None), ("photo_on_file", lambda m: bool(m.photo)),
                   "declarations", "membership_number", "submitted_at", "activated_at", "decision_reason", "cancelled_at", "cancel_reason",
                   "created_at", "geo_source", ("chosen_place", lambda m: _place(m))]),
        "events": [pick(e, ["status_from", "status_to", "note", "created_at"]) for e in m.events.all().order_by("created_at")],
        "payments": [pick(p, ["reference", "amount_kobo", "refunded_kobo", "currency", "status", "payer_email", "created_at", "paid_at"])
                     for p in m.payments.all().order_by("created_at")],
    }}
