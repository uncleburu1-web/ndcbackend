from apps.privacy.exporting import pick

DESCRIPTION = "Donations you made while signed in, with the details on your receipts."


def export_for(user) -> dict:
    from .models import Donation

    rows = Donation.objects.filter(donor_user=user).select_related("candidate").order_by("created_at")
    return {"donations": [pick(d, ["reference", "amount_kobo", "refunded_kobo", "currency", "designation",
                                   ("candidate", lambda d: d.candidate.full_name if d.candidate_id else None), "election_year", "donor_name",
                                   "donor_email", "donor_phone", "donor_state", "donor_address", "display_choice", "public_label", "status",
                                   "created_at", "paid_at", "refunded_at"]) for d in rows]}
