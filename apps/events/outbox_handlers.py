"""Background jobs (spec 18 §12.4). Payloads hold IDs only."""
from apps.core.outbox import handler


@handler("events_fanout")
def events_fanout(payload):
    """Material changes, organiser updates, cancellations and removals → event_update (registered and waitlisted)."""
    from apps.notifications.services import deliver_event

    from .models import Event

    e = Event.objects.filter(pk=payload["event"]).first()
    if e is not None:
        deliver_event(e, "event_update", payload.get("decision", ""))


@handler("events_reminders")
def events_reminders(payload):
    from apps.notifications.services import deliver_event

    from .models import Event, EventStatus

    e = Event.objects.filter(pk=payload["event"], status=EventStatus.PUBLISHED).first()
    if e is not None:
        deliver_event(e, "event_reminder", payload.get("which", "day"))


@handler("candidate_event_fanout")
def candidate_event_fanout(payload):
    from apps.notifications.services import deliver_candidate_event

    from .models import Event

    e = Event.objects.filter(pk=payload["event"]).select_related("candidate").first()
    if e is not None and e.candidate_id:
        deliver_candidate_event(e)
