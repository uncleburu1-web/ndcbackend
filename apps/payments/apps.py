from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    """Shared payment provider integration (spec 06 §6.3): donations (NDC-) and membership fees (MEM-)."""

    name = "apps.payments"
    label = "payments"
