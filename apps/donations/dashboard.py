"""Donation figures for the admin home (spec 11 §6.1). Finance viewers only; totals, never donors."""
from datetime import timedelta

from django.db.models import Count, Min, Q, Sum

from apps.access.models import Role
from apps.access.services import has_role
from apps.dashboard.cards import Card, Stat, weeks

from .models import Designation, Donation, DonationStatus


def admin_summary(user, *, now):
    if not has_role(user, Role.FINANCE_OFFICER):
        return []
    this, last = weeks(now)
    month = now - timedelta(days=30)
    review = Donation.objects.filter(status=DonationStatus.REVIEW)
    ok = Donation.objects.filter(status=DonationStatus.SUCCESSFUL)

    def agg(qs):
        return qs.aggregate(
            n30=Count("id", filter=Q(paid_at__gte=month)), nw=Count("id", filter=Q(paid_at__gte=this)),
            nl=Count("id", filter=Q(paid_at__gte=last, paid_at__lt=this)),
            k30=Sum("amount_kobo", filter=Q(paid_at__gte=month)), kw=Sum("amount_kobo", filter=Q(paid_at__gte=this)),
            kl=Sum("amount_kobo", filter=Q(paid_at__gte=last, paid_at__lt=this)))

    a, party, cand = agg(ok), agg(ok.filter(designation=Designation.PARTY)), agg(ok.filter(designation=Designation.CANDIDATE))
    return [
        Card("donations_review", "Donations under review", review.count(), "/admin/donations?status=review",
             oldest_at=review.aggregate(t=Min("created_at"))["t"], any_is_warning=True),
        Stat("donations", "count", "Successful donations (30 days)", a["n30"], a["nw"], a["nl"]),
        Stat("donations", "amount", "Given (30 days)", a["k30"] or 0, a["kw"] or 0, a["kl"] or 0, kobo=True),
        Stat("donations", "party", "To the NDC general fund (30 days)", party["k30"] or 0, party["kw"] or 0, party["kl"] or 0, kobo=True),
        Stat("donations", "candidates", "To candidates (30 days)", cand["k30"] or 0, cand["kw"] or 0, cand["kl"] or 0, kobo=True),
    ]
