from django.core.management.base import BaseCommand

from apps.access.services import deactivate_user

from ._shared import ACTOR, add_user_args, find_user


class Command(BaseCommand):
    help = "Deactivate a user locally and ban them in Clerk. Re-run to retry a failed Clerk ban."

    def add_arguments(self, parser):
        add_user_args(parser)
        parser.add_argument("--reason", default="")

    def handle(self, *args, **opts):
        user = find_user(opts)
        banned = deactivate_user(user=user, reason=opts["reason"], actor_label=ACTOR)
        self.stdout.write(self.style.SUCCESS(f"{user} is deactivated: API access blocked."))
        if banned:
            self.stdout.write(self.style.SUCCESS("Clerk ban confirmed: they can't sign in."))
        else:
            self.stdout.write(self.style.WARNING("Clerk ban FAILED (logged). Local block holds. Re-run this command to retry."))
