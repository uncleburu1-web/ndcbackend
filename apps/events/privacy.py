"""Spec 18 §17.4: your event registrations (event, status, ticket code, check-in)."""
from apps.privacy.exporting import pick

DESCRIPTION = "The events you registered for, with your ticket codes and check-in times."


def export_for(user) -> dict:
    from .models import EventRegistration

    regs = EventRegistration.objects.filter(user=user).select_related("event__candidate").order_by("created_at")
    return {"registrations": [pick(r, [("event", lambda r: r.event.title), ("organiser", lambda r: r.event.organiser_label),
                                       ("starts_at", lambda r: r.event.starts_at), "status", "ticket_code", "waitlist_position",
                                       "checked_in_at", "created_at"]) for r in regs]}
