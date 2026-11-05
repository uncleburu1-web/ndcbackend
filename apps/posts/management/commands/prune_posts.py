from datetime import timedelta

from django.utils import timezone

from apps.core.history import allow_history_delete
from apps.news import images
from apps.moderation.services import held_ids
from apps.posts.models import CandidatePost
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    job = "prune_posts"
    help = "Retention (spec 09 §10): purge deleted and removed posts after 1 year, except items under a legal hold (spec 13). Run daily."

    def run(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=365)
        old = CandidatePost.objects.filter(status="deleted", deleted_at__lt=cutoff) | CandidatePost.objects.filter(status="removed",
                                                                                                                  removed_at__lt=cutoff)
        old = old.exclude(pk__in=held_ids("post"))
        files = list(old.values_list("image", "image_thumb"))
        with allow_history_delete():  # spec 16 §6.3
            n, _ = old.delete()
        for pair in files:
            images.delete(*pair)
        text = f"Deleted {n} rows with old posts"
        self.stdout.write(text + ".")
        return text
