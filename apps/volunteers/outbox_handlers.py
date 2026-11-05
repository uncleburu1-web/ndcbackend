"""Background jobs (spec 18 §12.4). Payloads hold IDs only."""
from apps.core.outbox import handler


@handler("volunteer_update_fanout")
def volunteer_update_fanout(payload):
    from apps.notifications.services import deliver_volunteer_update

    from .models import VolunteerUpdate

    u = VolunteerUpdate.objects.filter(pk=payload["update"]).first()
    if u is not None:
        n = deliver_volunteer_update(u)
        VolunteerUpdate.objects.filter(pk=u.pk).update(sent_to=n)
