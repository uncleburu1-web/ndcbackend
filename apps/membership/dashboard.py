"""Membership figures for the admin home (spec 11 §6.1). Counts only."""
from django.db.models import Max, Min

from apps.access.models import Role
from apps.access.services import has_role
from apps.dashboard.cards import Card, Stat, weeks

from .models import Membership, MembershipPayment, MembershipStatus, PaymentStatus


def admin_summary(user, *, now):
    if not has_role(user, Role.MEMBERSHIP_OFFICER):
        return []
    this, last = weeks(now)
    waiting = Membership.objects.filter(status=MembershipStatus.PENDING_APPROVAL).annotate(paid=Max("payments__paid_at"))
    oldest = waiting.aggregate(t=Min("paid"))["t"] or waiting.aggregate(t=Min("updated_at"))["t"]
    review = MembershipPayment.objects.filter(status=PaymentStatus.REVIEW)
    active = Membership.objects.filter(status=MembershipStatus.ACTIVE)
    return [
        Card("membership_approvals", "Membership approvals", waiting.count(), "/admin/membership?status=pending_approval",
             oldest_at=oldest, target_working_days=3),
        Card("membership_payments_review", "Membership payments to check", review.count(), "/admin/membership/payments?status=review",
             oldest_at=review.aggregate(t=Min("created_at"))["t"], any_is_warning=True),
        Stat("membership", "active", "Active members", active.count(), active.filter(activated_at__gte=this).count(),
             active.filter(activated_at__gte=last, activated_at__lt=this).count()),
    ]
