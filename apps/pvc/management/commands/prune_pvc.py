from django.core.management.base import BaseCommand

from apps.pvc.services import prune


class Command(BaseCommand):
    help = "Delete VINs of PVC submissions rejected more than 90 days ago (spec 06 §10). Run daily."

    def handle(self, *args, **opts):
        self.stdout.write(self.style.SUCCESS(f"VINs removed: {prune()}"))
