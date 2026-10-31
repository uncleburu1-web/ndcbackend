from django.core.management.base import BaseCommand, CommandError

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import RoleError, revoke_role

from ._shared import ACTOR, add_user_args, find_user


class Command(BaseCommand):
    help = "Revoke an active role from a user (audited as system:cli)."

    def add_arguments(self, parser):
        add_user_args(parser)
        parser.add_argument("role", choices=[r.value for r in Role])
        parser.add_argument("--scope-type", default=ScopeType.GLOBAL, choices=[s.value for s in ScopeType])
        parser.add_argument("--scope-id", default=None)
        parser.add_argument("--reason", default="")

    def handle(self, *args, **opts):
        user = find_user(opts)
        assignment = RoleAssignment.objects.filter(
            user=user, role=opts["role"], scope_type=opts["scope_type"], scope_id=opts["scope_id"], is_active=True
        ).first()
        if not assignment:
            raise CommandError("That user has no active assignment for that role and scope.")
        try:
            revoke_role(assignment=assignment, reason=opts["reason"], actor_label=ACTOR)
        except RoleError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Revoked {assignment.role} from {user}."))
