from apps.core import outbox
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    help = "Delete done outbox jobs after 30 days and failed ones after 90 (spec 15 §9.3)."
    job = "prune_outbox"

    def run(self, *args, **opts):
        return f"Removed {outbox.prune()} jobs"
