from django.core.management.base import BaseCommand, CommandError

from apps.access.models import Role, ScopeType
from apps.access.services import RoleError, grant_role

from ._shared import ACTOR, add_user_args, find_user


class Command(BaseCommand):
    help = "Grant a role to a user (audited as system:cli). Example: grant_role ada@example.com system_admin"

    def add_arguments(self, parser):
        add_user_args(parser)
        parser.add_argument("role", choices=[r.value for r in Role])
        parser.add_argument("--scope-type", default=ScopeType.GLOBAL, choices=[s.value for s in ScopeType])
        parser.add_argument("--scope-id", default=None)
        parser.add_argument("--note", default="")

    def handle(self, *args, **opts):
        user = find_user(opts)
        try:
            a = grant_role(user=user, role=opts["role"], scope_type=opts["scope_type"], scope_id=opts["scope_id"],
                           note=opts["note"], actor_label=ACTOR)
        except RoleError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Granted {a.role} ({a.scope_type}) to {user}. Assignment {a.id}"))
