from django.core.management.base import CommandError

from apps.core.jobs import TrackedCommand
from apps.notifications.services import deliver_post
from apps.posts.models import CandidatePost


class Command(TrackedCommand):
    job = "deliver_post"
    help = "Re-run follower delivery for one candidate post (idempotent; spec 09 §11)."

    def add_arguments(self, parser):
        parser.add_argument("post_id")

    def run(self, *args, **opts):
        p = CandidatePost.objects.select_related("candidate").filter(pk=opts["post_id"]).first()
        if p is None:
            raise CommandError("No post with that id.")
        n = deliver_post(p)
        self.stdout.write(f"Delivered to {n} new recipients.")
        return f"Delivered to {n} new recipients"
