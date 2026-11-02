from apps.pvc.services import prune
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    job = "prune_pvc"
    help = "Delete VINs of PVC submissions rejected more than 90 days ago (spec 06 §10). Run daily."

    def run(self, *args, **opts):
        n = prune()
        self.stdout.write(self.style.SUCCESS(f"VINs removed: {n}"))
        return f"{n} VINs removed"
