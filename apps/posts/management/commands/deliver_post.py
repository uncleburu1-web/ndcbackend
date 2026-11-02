from django.core.management.base import BaseCommand, CommandError

from apps.notifications.services import deliver_post
from apps.posts.models import CandidatePost


class Command(BaseCommand):
    help = "Re-run follower delivery for one candidate post (idempotent; spec 09 §11)."

    def add_arguments(self, parser):
        parser.add_argument("post_id")

    def handle(self, *args, **opts):
        p = CandidatePost.objects.select_related("candidate").filter(pk=opts["post_id"]).first()
        if p is None:
            raise CommandError("No post with that id.")
        self.stdout.write(f"Delivered to {deliver_post(p)} new recipients.")
