from django.core.management.base import CommandError

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    help = 'Record a quarterly restore test (spec 14 §4.11): manage.py restore_drill --ok "notes"'
    job = "restore_drill"

    def add_arguments(self, parser):
        parser.add_argument("--ok", dest="notes", required=True, help="What was restored and checked (no personal data).")

    def run(self, *args, notes="", **opts):
        notes = (notes or "").strip()
        if len(notes) < 5:
            raise CommandError("Describe the restore test (at least 5 characters).")
        record(AuditLog.Action.SECURITY_RESTORE_DRILL, actor_label="system:cli", notes=notes[:200])
        return "Restore test recorded"
