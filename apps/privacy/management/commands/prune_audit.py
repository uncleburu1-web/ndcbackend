"""Spec 14 §4.8, §10: audit entries, closed privacy requests and closed incidents older than 6 years.
The last removed audit row's hash becomes the chain anchor, so verify_audit still works."""
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import CHAIN_LOCK
from apps.core.jobs import TrackedCommand
from apps.privacy.models import AuditAnchor, ConsentRecord, DataRequest, Incident


class Command(TrackedCommand):
    help = "Prune audit and privacy records past their retention (6 years)."
    job = "prune_audit"

    def run(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=settings.RECORD_RETENTION_DAYS)
        with transaction.atomic():
            with connection.cursor() as c:
                c.execute("SELECT pg_advisory_xact_lock(%s)", [CHAIN_LOCK])
            old = AuditLog.objects.filter(created_at__lt=cutoff, seq__isnull=False).order_by("-seq")
            last = old.first()
            n = 0
            if last is not None:
                AuditAnchor.objects.update_or_create(seq=last.seq, defaults={"hash": last.hash})
                AuditAnchor.objects.filter(seq__lt=last.seq).delete()
                n, _ = AuditLog.objects.filter(seq__lte=last.seq).delete()
            n += AuditLog.objects.filter(created_at__lt=cutoff, seq__isnull=True).delete()[0]
        r, _ = DataRequest.objects.filter(closed_at__lt=cutoff).delete()
        i, _ = Incident.objects.filter(closed_at__lt=cutoff).delete()
        c_, _ = ConsentRecord.objects.filter(withdrawn_at__lt=cutoff).delete()
        return f"Removed {n} audit entries, {r} requests, {i} incidents, {c_} withdrawn consents"
