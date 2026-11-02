from apps.notifications.services import prune
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    job = "prune_notifications"
    help = "Delete read notifications older than 180 days and unread ones older than 365 (spec 07 §10). Run daily."

    def run(self, *args, **opts):
        n = prune()
        self.stdout.write(f"Deleted {n} notifications.")
        return f"Deleted {n} notifications"
