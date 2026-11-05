from apps.core.jobs import TrackedCommand
from apps.volunteers import services


class Command(TrackedCommand):
    help = "Delete finished volunteer applications after 90 days and updates after a year (spec 18 §17.2). Daily."
    job = "prune_volunteers"

    def run(self, *args, **opts):
        r = services.prune()
        return f"{r['applications']} applications and {r['updates']} updates deleted"
