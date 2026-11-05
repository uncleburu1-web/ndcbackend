
from django.utils import timezone

from apps.core.jobs import TrackedCommand
from apps.communities import retention


class Command(TrackedCommand):
    help = "Retention for communities (spec 17 §17.3). Run daily."
    job = "prune_communities"

    def run(self, *args, **opts):
        r = retention.prune(timezone.now())
        return (f"erased {r['erased']} deleted texts, deleted {r['deleted_items']} old posts and comments, {r['memberships']} ended "
                f"memberships, {r['edits']} old edit records")
