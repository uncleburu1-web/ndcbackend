"""Privacy and security cards for the admin home (spec 14 §4.13). Counts only."""
from datetime import timedelta

from django.conf import settings
from django.db.models import Min

from apps.access.models import Role
from apps.access.services import has_role, is_system_admin
from apps.dashboard.cards import Card

from . import checks, incidents
from .models import AccessReview, DataRequest

CACHEABLE = False


def access_review_due(now):
    last = AccessReview.objects.order_by("-completed_at").first()
    if last is None:
        from apps.access.models import RoleAssignment

        first = RoleAssignment.objects.order_by("created_at").values_list("created_at", flat=True).first()
        return first is not None and now - first > timedelta(days=settings.ACCESS_REVIEW_DAYS)
    return now - last.completed_at > timedelta(days=settings.ACCESS_REVIEW_DAYS)


def admin_summary(user, *, now):
    out = []
    sysadmin = is_system_admin(user)
    officer = has_role(user, Role.PRIVACY_OFFICER)
    if officer:
        open_reqs = DataRequest.objects.filter(status=DataRequest.Status.OPEN, self_served=False)
        first_due = open_reqs.aggregate(t=Min("due_at"))["t"]
        days = settings.PRIVACY_REQUEST_DAYS
        # amber halfway through the 30 days, red once the oldest is overdue
        out.append(Card("privacy_requests_due", "Privacy requests due", open_reqs.count(), "/admin/privacy",
                        oldest_at=first_due - timedelta(days=days) if first_due else None, target_hours=days * 12))
    if officer or sysadmin:
        clocks = incidents.open_clocks(now)
        first = min((c["due_at"] for _, c in clocks), default=None)
        out.append(Card("incident_deadlines", "Incident deadlines", len(clocks), "/admin/privacy/incidents",
                        oldest_at=first - timedelta(hours=incidents.CLOCK_HOURS) if first else None,
                        target_hours=incidents.CLOCK_HOURS // 2))
    if sysadmin:
        if access_review_due(now):
            out.append(Card("access_review_due", "Access review due", 1, "/admin/team/review", any_is_warning=True))
        state, _ = checks.audit_state(now)
        if state == checks.FAIL:
            out.append(Card("audit_chain_broken", "Audit log chain broken", 1, "/admin/audit", oldest_at=now - timedelta(days=365),
                            target_hours=1))
    return out
