from datetime import timedelta

from django.utils import timezone

from apps.news import images
from apps.posts.models import CandidatePost, PostReport
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    job = "prune_posts"
    help = "Retention (spec 09 §10): purge deleted and removed posts after 1 year, and dismissed reports after 1 year. Run daily."

    def run(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=365)
        old = CandidatePost.objects.filter(status="deleted", deleted_at__lt=cutoff) | CandidatePost.objects.filter(status="removed",
                                                                                                                  removed_at__lt=cutoff)
        files = list(old.values_list("image", "image_thumb"))
        n, _ = old.delete()
        for pair in files:
            images.delete(*pair)
        r, _ = PostReport.objects.filter(status="dismissed", resolved_at__lt=cutoff).delete()
        text = f"Deleted {n} rows with old posts and {r} dismissed reports"
        self.stdout.write(text + ".")
        return text
