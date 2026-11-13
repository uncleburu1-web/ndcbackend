from django.core.management.base import CommandError

from apps.core.jobs import TrackedCommand
from apps.news.models import Article
from apps.notifications.services import deliver_announcement


class Command(TrackedCommand):
    job = "deliver_announcement"
    help = "Re-run announcement delivery for one article (idempotent; spec 07 §11)."

    def add_arguments(self, parser):
        parser.add_argument("article_id")

    def run(self, *args, **opts):
        a = Article.objects.filter(pk=opts["article_id"], source="official", status="successful").first()
        if a is None:
            raise CommandError("No published official announcement with that id.")
        n = deliver_announcement(a)
        self.stdout.write(f"Delivered to {n} new recipients.")
        return f"Delivered to {n} new recipients"
