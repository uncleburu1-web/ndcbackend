import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class DonationsConfig(AppConfig):
    name = "apps.donations"
    label = "donations"

    def ready(self):
        # Start-up check (spec 05 §6.6): say loudly if donations are switched on but the gate can't open.
        from django.conf import settings

        if getattr(settings, "DONATIONS_ENABLED", False):
            from .services import gate_status

            failing = [k for k, ok in gate_status().items() if not ok]
            if failing:
                logger.warning("DONATIONS_ENABLED is true but donations stay closed: %s", ", ".join(failing))
