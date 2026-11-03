from django.core.management.base import CommandError

from apps.audit.services import verify_chain
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    help = "Walk the audit hash chain (spec 14 §4.8). Weekly."
    job = "verify_audit"

    def run(self, *args, **opts):
        r = verify_chain()
        if not r["ok"]:
            where = f"chain broken at #{r['broken_at']}" if r["broken_at"] else f"{r['unhashed']} unhashed rows"
            raise CommandError(where)
        return f"Intact: {r['checked']} rows checked"
