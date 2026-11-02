from apps.payments.dispatch import sweep_all
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    job = "sweep_payments"
    help = "Re-verify pending donations and membership payments, abandon 24h-old ones, clear old IP hashes. Run every 15 minutes."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-minutes", type=int, default=10)

    def run(self, *args, **opts):
        counts = sweep_all(older_than_minutes=opts["older_than_minutes"])
        text = ", ".join(f"{k}: {v}" for k, v in counts.items())
        self.stdout.write(self.style.SUCCESS(text))
        return text
