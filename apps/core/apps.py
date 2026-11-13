from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "apps.core"
    label = "core"

    def ready(self):
        from .throttling import install

        install()  # spec 15 §11.4: throttles fail open when the cache is down

        from .i18n import connect_signals

        connect_signals()  # spec 21 §11: people's words saved as NFC
