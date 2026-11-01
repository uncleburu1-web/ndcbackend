from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.news import images
from apps.news.models import Article, ArticleReport


class Command(BaseCommand):
    help = ("Retention (spec 07 §10): delete rejected/withdrawn articles after 180 days, removed articles and dismissed "
            "reports after 1 year. Run daily.")

    def handle(self, *args, **opts):
        now = timezone.now()
        old = (Article.objects.filter(status__in=["rejected", "withdrawn"], updated_at__lt=now - timedelta(days=180))
               | Article.objects.filter(status="removed", removed_at__lt=now - timedelta(days=365)))
        files = list(old.values_list("image", "image_thumb"))
        n_articles, _ = old.delete()  # events/reports/notifications cascade (QuerySet.delete bypasses the event guard)
        for pair in files:
            images.delete(*pair)
        n_reports, _ = ArticleReport.objects.filter(status="dismissed", resolved_at__lt=now - timedelta(days=365)).delete()
        self.stdout.write(f"Deleted {n_articles} rows with old articles and {n_reports} dismissed reports.")
