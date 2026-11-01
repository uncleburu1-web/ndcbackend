from django.core.management.base import BaseCommand

from apps.notifications.services import prune


class Command(BaseCommand):
    help = "Delete read notifications older than 180 days and unread ones older than 365 (spec 07 §10). Run daily."

    def handle(self, *args, **opts):
        self.stdout.write(f"Deleted {prune()} notifications.")
