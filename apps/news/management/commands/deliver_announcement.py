from django.core.management.base import BaseCommand, CommandError

from apps.news.models import Article
from apps.notifications.services import deliver_announcement


class Command(BaseCommand):
    help = "Re-run announcement delivery for one article (idempotent; spec 07 §11)."

    def add_arguments(self, parser):
        parser.add_argument("article_id")

    def handle(self, *args, **opts):
        a = Article.objects.filter(pk=opts["article_id"], source="official", status="successful").first()
        if a is None:
            raise CommandError("No published official announcement with that id.")
        self.stdout.write(f"Delivered to {deliver_announcement(a)} new recipients.")
