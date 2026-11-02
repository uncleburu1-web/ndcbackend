from django.apps import AppConfig


class SearchConfig(AppConfig):
    name = "apps.search"
    label = "search"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401  keep the index in step with every save (spec 12 §6.1)
