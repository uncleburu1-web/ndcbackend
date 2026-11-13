"""
python manage.py rebuild_search_text

Spec 21 §12: after `fold()` learned Hausa's ɓ ɗ ƙ ƴ (and ɛ ɔ), stored folded text and search vectors are rebuilt.
The same work as `rebuild_search` (spec 12), under the name the spec gives it. Safe to run again.
"""
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Rebuild stored folded text and search vectors (alias of rebuild_search, spec 21 §12)."

    def handle(self, *args, **opts):
        call_command("rebuild_search", stdout=self.stdout)
