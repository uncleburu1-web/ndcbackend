from django.core.management.base import BaseCommand

from apps.payments.dispatch import sweep_all


class Command(BaseCommand):
    help = "Re-verify pending donations and membership payments, abandon 24h-old ones, clear old IP hashes. Run every 15 minutes."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=10)

    def handle(self, *args, **opts):
        counts = sweep_all(older_than_minutes=opts["older_than_minutes"])
        self.stdout.write(self.style.SUCCESS(", ".join(f"{k}: {v}" for k, v in counts.items())))
