"""PVC figures for the admin home (spec 11 §6.1). Counts only; per-user (own check excluded), so not cached."""
from django.db.models import Min

from apps.access.models import Role
from apps.access.services import has_role
from apps.dashboard.cards import Card, Stat, weeks

from .models import PvcStatus, PvcVerification

CACHEABLE = False


def admin_summary(user, *, now):
    if not (has_role(user, Role.PVC_OFFICER) or has_role(user, Role.MEMBERSHIP_OFFICER)):
        return []
    this, last = weeks(now)
    pending = PvcVerification.objects.filter(status=PvcStatus.PENDING).exclude(user=user)
    all_pending = PvcVerification.objects.filter(status=PvcStatus.PENDING)
    verified = PvcVerification.objects.filter(status=PvcStatus.SUCCESSFUL)
    rejected = PvcVerification.objects.filter(status=PvcStatus.REJECTED)
    return [
        Card("pvc_pending", "PVC checks", pending.count(), "/admin/pvc", oldest_at=pending.aggregate(t=Min("created_at"))["t"],
             target_working_days=3, per_user=True),
        Stat("pvc", "verified", "Verified", verified.count(), verified.filter(reviewed_at__gte=this).count(),
             verified.filter(reviewed_at__gte=last, reviewed_at__lt=this).count()),
        Stat("pvc", "pending", "Waiting for a check", all_pending.count()),
        Stat("pvc", "rejected", "Rejected", rejected.count(), rejected.filter(reviewed_at__gte=this).count(),
             rejected.filter(reviewed_at__gte=last, reviewed_at__lt=this).count()),
    ]
