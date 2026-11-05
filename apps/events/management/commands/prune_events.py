from apps.core.jobs import TrackedCommand
from apps.events import services


class Command(TrackedCommand):
    help = "Delete registrations 90 days after an event and events 2 years after (spec 18 §17.3). Daily."
    job = "prune_events"

    def run(self, *args, **opts):
        r = services.prune()
        return f"{r['registrations']} registrations and {r['events']} events deleted"
