"""Moderation figures for the admin home (spec 13 §4.9). Per-user (what the viewer can act on), so not cached."""
from django.db.models import Min

from apps.access.models import Role
from apps.access.services import has_role, is_system_admin
from apps.dashboard.cards import Card

from . import rules as R
from .models import Appeal, CaseStatus, LegalNotice

CACHEABLE = False


def admin_summary(user, *, now):
    from . import services

    sysadmin = is_system_admin(user)
    if not (sysadmin or has_role(user, Role.MODERATOR) or has_role(user, Role.NEWS_REVIEWER)):
        return []
    cases = services.visible_cases(user).exclude(status=CaseStatus.CLOSED)
    if not sysadmin:
        cases = cases.filter(status=CaseStatus.OPEN)
    oldest = cases.order_by("opened_at").first()
    out = [Card("reports_review", "Reports to review", cases.count(), "/admin/moderation", oldest_at=oldest.opened_at if oldest else None,
                target_hours=R.TARGET_HOURS[oldest.priority] if oldest else None, per_user=True)]
    urgent = cases.filter(priority__in=["urgent", "legal"])
    out.append(Card("reports_urgent", "Urgent reports", urgent.count(), "/admin/moderation?priority=urgent",
                    oldest_at=urgent.aggregate(t=Min("opened_at"))["t"], target_hours=24, per_user=True))
    pending = [a for a in Appeal.objects.filter(status=Appeal.Status.PENDING).select_related("case", "restriction__case")
               if services.can_decide_appeal(user, a)]
    out.append(Card("appeals_waiting", "Appeals waiting", len(pending), "/admin/moderation?tab=appeals",
                    oldest_at=min((a.created_at for a in pending), default=None), target_hours=72, per_user=True))
    if sysadmin:
        due = LegalNotice.objects.filter(status="open", due_at__isnull=False)
        # target_hours=24 so the card is amber halfway to the 48-hour deadline and red once it has passed (spec 13 §11)
        out.append(Card("legal_due", "Legal notices due", due.count(), "/admin/moderation/legal",
                        oldest_at=due.aggregate(t=Min("received_at"))["t"], target_hours=24))
    return out
