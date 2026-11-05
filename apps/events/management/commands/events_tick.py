from apps.core.jobs import TrackedCommand
from apps.events import services


class Command(TrackedCommand):
    help = "Event reminders, registration deadlines, and campaign events a new blackout window now covers (spec 18 §12.3). Every 15 minutes."
    job = "events_tick"

    def run(self, *args, **opts):
        r = services.tick()
        return (f"{r['reminders_day']} day and {r['reminders_hour']} hour reminders queued; {r['closed']} registrations closed; "
                f"{r['flagged']} events flagged for a blackout window")
