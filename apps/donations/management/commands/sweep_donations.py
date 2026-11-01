from django.core.management.base import BaseCommand

from apps.donations.services import sweep


class Command(BaseCommand):
    help = "Re-verify pending donations, mark 24h-old ones abandoned, clear 90-day-old IP hashes (spec 05 §6.5). Run every 15 minutes."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=10)

    def handle(self, *args, **opts):
        counts = sweep(older_than_minutes=opts["older_than_minutes"])
        self.stdout.write(self.style.SUCCESS(", ".join(f"{k}: {v}" for k, v in counts.items())))
