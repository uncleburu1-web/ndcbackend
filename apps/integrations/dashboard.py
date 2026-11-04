"""Spec 15 §9.3: "Background tasks failing" on the admin home (system admins). Counts only."""
from apps.access.services import is_system_admin
from apps.core import outbox
from apps.dashboard.cards import Card

CACHEABLE = False


def admin_summary(user, *, now):
    if not is_system_admin(user):
        return []
    st = outbox.stats(now)
    return [Card("background_tasks_failing", "Background tasks failing", st["failed"], "/admin/integrations",
                 oldest_at=st["oldest_failed_at"], any_is_warning=True)]
