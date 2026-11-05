from datetime import timedelta

from django.utils import timezone

from apps.core.history import allow_history_delete
from apps.news import images
from apps.moderation.services import held_ids
from apps.news.models import Article
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    job = "prune_news"
    help = ("Retention (spec 07 §10): delete rejected/withdrawn articles after 180 days, removed articles after 1 year, except "
            "items under a legal hold (spec 13). Reports are pruned by prune_moderation. Run daily.")

    def run(self, *args, **opts):
        now = timezone.now()
        old = (Article.objects.filter(status__in=["rejected", "withdrawn"], updated_at__lt=now - timedelta(days=180))
               | Article.objects.filter(status="removed", removed_at__lt=now - timedelta(days=365))).exclude(pk__in=held_ids("article"))
        files = list(old.values_list("image", "image_thumb"))
        with allow_history_delete():  # spec 16 §6.3: retention may delete history
            n_articles, _ = old.delete()  # events/notifications cascade
        for pair in files:
            images.delete(*pair)
        text = f"Deleted {n_articles} rows with old articles"
        self.stdout.write(text + ".")
        return text
