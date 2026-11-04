from datetime import timedelta

from django.utils import timezone

from apps.core.jobs import TrackedCommand
from apps.integrations.models import WebhookReceipt


class Command(TrackedCommand):
    help = "Delete webhook receipts older than 90 days (spec 15 §12.4)."
    job = "prune_webhooks"

    def run(self, *args, **opts):
        n, _ = WebhookReceipt.objects.filter(received_at__lt=timezone.now() - timedelta(days=90)).delete()
        return f"Removed {n} receipts"
