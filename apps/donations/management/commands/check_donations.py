from django.conf import settings
from django.core.management.base import BaseCommand

from apps.payments.providers import ProviderError, get_provider
from apps.donations.services import gate_status


class Command(BaseCommand):
    help = "Show whether the donations compliance gate is open (spec 05 §6.6), and test the provider key. Never prints keys."

    def handle(self, *args, **opts):
        for name, ok in gate_status().items():
            self.stdout.write(f"{'OK  ' if ok else 'FAIL'} {name}")
        if settings.PAYMENT_PROVIDER == "paystack" and settings.PAYSTACK_SECRET_KEY:
            mode = "LIVE" if settings.PAYSTACK_SECRET_KEY.startswith("sk_live_") else "test"
            try:
                get_provider().health()
                self.stdout.write(f"OK   Paystack {mode} key accepted")
            except ProviderError as exc:
                self.stdout.write(f"FAIL Paystack {mode} key: {exc}")
        open_ = all(gate_status().values())
        self.stdout.write(self.style.SUCCESS("Donations are OPEN") if open_ else self.style.WARNING("Donations are CLOSED"))
