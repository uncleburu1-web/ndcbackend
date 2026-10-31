from django.core.management.base import BaseCommand, CommandError

from apps.access.services import RoleError, reactivate_user

from ._shared import ACTOR, add_user_args, find_user


class Command(BaseCommand):
    help = "Reactivate a deactivated user and lift the Clerk ban."

    def add_arguments(self, parser):
        add_user_args(parser)
        parser.add_argument("--reason", default="")

    def handle(self, *args, **opts):
        user = find_user(opts)
        try:
            unbanned = reactivate_user(user=user, reason=opts["reason"], actor_label=ACTOR)
        except RoleError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"{user} is active again."))
        if not unbanned:
            self.stdout.write(self.style.WARNING("Clerk unban FAILED (logged). Re-run this command to retry."))
