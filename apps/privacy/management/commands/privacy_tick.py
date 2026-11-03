"""Hourly privacy upkeep (spec 14 §13): counts only. Clocks are shown, never acted on (rule 7)."""
from django.utils import timezone

from apps.core.jobs import TrackedCommand
from apps.privacy import incidents
from apps.privacy.models import DataRequest


class Command(TrackedCommand):
    help = "Privacy upkeep: report overdue privacy requests and incident clocks."
    job = "privacy_tick"

    def run(self, *args, **opts):
        now = timezone.now()
        overdue = DataRequest.objects.filter(status=DataRequest.Status.OPEN, due_at__lt=now).count()
        clocks = incidents.open_clocks(now)
        late = sum(1 for _, c in clocks if c["state"] == "overdue")
        return f"{overdue} requests overdue, {len(clocks)} incident clocks open ({late} overdue)"
