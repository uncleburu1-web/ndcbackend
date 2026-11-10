"""Spec 19 §8.1, §12.2: send `geo_confirm` to people whose membership or PVC location isn't confirmed yet, each record
once, at most 500 a run (hourly). Counts only in the output."""
from apps.core.jobs import TrackedCommand
from apps.geo.records import BATCH, notify_unconfirmed


class Command(TrackedCommand):
    help = "Ask people with an unconfirmed membership or PVC location to confirm where they vote (500 a run)."
    job = "geo_notify_unconfirmed"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=BATCH)

    def run(self, *args, limit=BATCH, **opts):
        r = notify_unconfirmed(limit=max(1, limit))
        return f"asked {r.get('membership', 0)} members and {r.get('pvc', 0)} PVC applicants"
