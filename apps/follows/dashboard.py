"""Follow figures for the admin home (spec 11 §6.1). Platform totals only — never who follows whom."""
from apps.access.services import is_system_admin
from apps.dashboard.cards import Stat, weeks

from .models import CandidateFollow


def admin_summary(user, *, now):
    if not is_system_admin(user):
        return []
    this, last = weeks(now)
    f = CandidateFollow.objects.all()
    on = f.filter(notify=True)
    return [
        Stat("candidates", "follows", "Follows", f.count(), f.filter(created_at__gte=this).count(),
             f.filter(created_at__gte=last, created_at__lt=this).count()),
        Stat("candidates", "bell_on", "Follows with the bell on", on.count()),
    ]
