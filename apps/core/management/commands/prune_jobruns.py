"""Delete job records older than 90 days (spec 11 §5.2). Schedule weekly."""
from apps.core.jobs import TrackedCommand, prune


class Command(TrackedCommand):
    help = "Delete background job records older than 90 days."
    job = "prune_jobruns"

    def run(self, *args, **opts):
        n = prune()
        self.stdout.write(f"Deleted {n} old job records.")
        return f"Deleted {n} old job records"
