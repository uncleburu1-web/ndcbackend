"""Privacy requests (spec 14 §4.4–4.5, §6.4): reference, 30-day clock (paused while waiting for the person), events, notifications."""
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from .models import DataRequest, DataRequestEvent, new_reference

S = DataRequest.Status
OPEN_STATES = (S.OPEN, S.WAITING)


def _err(msg, code, status=400):
    return ServiceError(msg, code, status)


def _notify(req, decision=""):
    from apps.notifications.models import Kind, Notification

    u = req.user
    if u is None or not u.is_active or u.clerk_deleted_at:
        return None
    return Notification.objects.create(recipient=u, kind=Kind.PRIVACY_REQUEST_UPDATE, data_request=req, decision=decision[:10])


def _event(req, event, actor=None, note="", shown=False):
    return DataRequestEvent.objects.create(request=req, event=event, actor=actor, note=note[:2000], shown_to_person=shown)


def due_for(received_at, paused_seconds=0):
    return received_at + timedelta(days=settings.PRIVACY_REQUEST_DAYS, seconds=paused_seconds)


def open_request(user, kind, details="", *, self_served=False):
    if kind not in DataRequest.Kind.values:
        raise _err("Choose what you're asking for.", "validation_error")
    details = (details or "").strip()
    if not self_served and kind == DataRequest.Kind.OTHER and len(details) < 10:
        raise _err("Tell us a little more (at least 10 characters).", "validation_error")
    now = timezone.now()
    status = S.DONE if self_served else S.OPEN
    for _ in range(5):
        try:
            with transaction.atomic():
                req = DataRequest.objects.create(reference=new_reference("PRV"), user=user, kind=kind, details=details[:2000], status=status,
                                                 self_served=self_served, received_at=now, due_at=due_for(now),
                                                 closed_at=now if self_served else None,
                                                 outcome="Downloaded from Your data." if self_served else "")
                _event(req, "self_served" if self_served else "opened", user, shown=True)
                if not self_served:
                    _notify(req, "opened")
                return req
        except IntegrityError:
            continue
    raise _err("Couldn't create a reference. Try again.", "reference_unavailable", 503)


def _locked(req_id):
    try:
        return DataRequest.objects.select_for_update().get(pk=req_id)
    except DataRequest.DoesNotExist:
        raise _err("Request not found.", "not_found", 404)


def _require(req, *states):
    if req.status not in states:
        raise _err("This request can't do that in its current state.", "invalid_transition", 409)


def _note(text, required=True, minimum=5):
    t = (text or "").strip()
    if required and len(t) < minimum:
        raise _err(f"Write at least {minimum} characters.", "validation_error")
    return t[:2000]


@transaction.atomic
def reply(req_id, by, text):
    req = _locked(req_id)
    _require(req, *OPEN_STATES)
    note = _note(text)
    if req.assigned_to_id is None:
        req.assigned_to = by
        req.save(update_fields=["assigned_to"])
    _event(req, "reply", by, note, shown=True)
    _notify(req, "reply")
    return req


@transaction.atomic
def ask_info(req_id, by, text):
    """Pauses the clock (GAID): the reason is shown to the person."""
    req = _locked(req_id)
    _require(req, S.OPEN)
    note = _note(text)
    req.status, req.paused_at = S.WAITING, timezone.now()
    req.save(update_fields=["status", "paused_at"])
    _event(req, "ask_info", by, note, shown=True)
    _notify(req, "waiting")
    return req


@transaction.atomic
def resume(req_id, by=None, text=""):
    """The person answered (or the officer resumes): the paused time moves the due date."""
    req = _locked(req_id)
    _require(req, S.WAITING)
    now = timezone.now()
    paused = int((now - req.paused_at).total_seconds()) if req.paused_at else 0
    req.paused_seconds += max(paused, 0)
    req.paused_at = None
    req.status = S.OPEN
    req.due_at = due_for(req.received_at, req.paused_seconds)
    req.save(update_fields=["paused_seconds", "paused_at", "status", "due_at"])
    by_person = by is not None and by.pk == req.user_id
    _event(req, "info_given" if by_person else "resumed", by, (text or "").strip()[:2000], shown=True)
    if not by_person:
        _notify(req, "resumed")
    return req


def answer(req_id, user, text):
    """The person replies to an ask-for-information: the clock restarts."""
    if not DataRequest.objects.filter(pk=req_id, user=user).exists():
        raise _err("Request not found.", "not_found", 404)
    _note(text)
    return resume(req_id, by=user, text=text)


@transaction.atomic
def close(req_id, by, outcome, text):
    if outcome not in ("done", "refused"):
        raise _err("Choose Done or Refused.", "validation_error")
    req = _locked(req_id)
    _require(req, *OPEN_STATES)
    note = _note(text, minimum=10)
    now = timezone.now()
    if req.paused_at:
        req.paused_seconds += int((now - req.paused_at).total_seconds())
        req.paused_at = None
    req.status = S.DONE if outcome == "done" else S.REFUSED
    req.outcome, req.closed_at = note, now
    req.save(update_fields=["status", "outcome", "closed_at", "paused_at", "paused_seconds"])
    _event(req, outcome, by, note, shown=True)
    _notify(req, outcome)
    record(AuditLog.Action.PRIVACY_REQUEST_CLOSED, target=req, actor=by, reference=req.reference, outcome=outcome)
    return req


def note_viewed(req, by, request=None):
    """Officer views of the person's account are audited once per request per day."""
    from django.core.cache import cache

    key = f"privacy:viewed:{req.pk}:{by.pk}:{timezone.localdate():%Y%m%d}"
    if cache.add(key, 1, 86400):
        record(AuditLog.Action.PRIVACY_REQUEST_VIEWED, target=req, actor=by, request=request, reference=req.reference)


def due_words(req, now=None):
    now = now or timezone.now()
    if req.status in (S.DONE, S.REFUSED):
        return "Closed"
    if req.status == S.WAITING:
        return "Paused: waiting for the person"
    days = (req.due_at - now).total_seconds() / 86400
    if days >= 1:
        n = int(days)
        return f"Due in {n} day{'s' if n != 1 else ''}"
    if days >= 0:
        return "Due today"
    n = max(int(-days), 1)
    return f"Overdue by {n} day{'s' if n != 1 else ''}"


def overdue(req, now=None):
    now = now or timezone.now()
    return req.status == S.OPEN and req.due_at < now


def payload(req, *, officer=False, now=None):
    events = req.events.all() if officer else req.events.filter(shown_to_person=True)
    out = {"id": str(req.pk), "reference": req.reference, "kind": req.kind, "kind_label": req.get_kind_display(), "details": req.details,
           "status": req.status, "status_label": req.get_status_display(), "received_at": req.received_at, "due_at": req.due_at,
           "due_words": due_words(req, now), "overdue": overdue(req, now), "closed_at": req.closed_at, "outcome": req.outcome,
           "self_served": req.self_served,
           "events": [{"event": e.event, "note": e.note, "at": e.created_at, "shown_to_person": e.shown_to_person,
                       **({"by": e.actor.full_name or e.actor.email if e.actor_id else ""} if officer else {})} for e in events]}
    if officer:
        u = req.user
        out["person"] = ({"id": str(u.pk), "name": u.full_name, "email": u.email, "deleted": bool(u.clerk_deleted_at)} if u else None)
        out["assigned_to"] = (req.assigned_to.full_name or req.assigned_to.email) if req.assigned_to_id else ""
    return out
