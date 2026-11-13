"""
Events — spec 18 §3–§8, §12.1. The one place that decides who may organise, what an event may say and when, and who may
register. Views and other apps call these functions only.
"""
import secrets
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import blackouts
from apps.core.exceptions import ServiceError
from apps.core.i18n import clean_content_language
from apps.news.services import clean_text

from .models import (
    CAMPAIGNING_TYPES,
    AddressVisibility,
    ChangeKind,
    Event,
    EventChange,
    EventRegistration,
    EventStatus,
    EventType,
    EventUpdate,
    MaterialChange,
    Mode,
    Organiser,
    Registration,
    RegistrationStatus,
    VenueType,
)

PURPOSE = "event_registration"
MAX_DAYS_LONG = 3
MAX_AHEAD = timedelta(days=366)
MAX_UPCOMING_PER_PERSON = 20
MAX_REREGISTER = 3
WAITLIST_CAP = 1000
MAX_UPDATES = 3
CHECKIN_BEFORE = CHECKIN_AFTER = timedelta(hours=24)
PAST_VISIBLE = timedelta(days=90)
TICKET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
PUBLIC_RALLY_TYPES = CAMPAIGNING_TYPES
MATERIAL_FIELDS = {"starts_at": MaterialChange.TIME, "ends_at": MaterialChange.TIME, "venue_name": MaterialChange.VENUE,
                   "venue_address": MaterialChange.VENUE, "mode": MaterialChange.MODE, "online_url": MaterialChange.LINK}
LIVE = (EventStatus.PUBLISHED, EventStatus.CANCELLED)  # what the public can open


def _err(msg, code, status=400, **extra):
    e = ServiceError(msg, code=code, status_code=status)
    if extra:
        e.extra = extra
    return e


def _signed_in(user) -> bool:
    return bool(user and getattr(user, "is_authenticated", False) and user.is_active)


def _when(dt) -> str:
    d = timezone.localtime(dt)
    return f"{d.day} {d:%b %Y}"


# ------------------------------------------------------------------ organisers (rule 1)
def candidate_published(candidate) -> bool:
    from apps.posts.services import candidate_published as cp

    return candidate is not None and cp(candidate)


def can_organise(user, candidate=None) -> bool:
    """Linked accounts of a published candidate; system admins for party events (candidate=None)."""
    if not _signed_in(user):
        return False
    if candidate is None:
        from apps.access.services import is_system_admin

        return is_system_admin(user)
    from apps.posts.services import can_post

    return can_post(user, candidate)


def require_organiser(user, event_or_candidate):
    candidate = event_or_candidate.candidate if isinstance(event_or_candidate, Event) else event_or_candidate
    party = isinstance(event_or_candidate, Event) and event_or_candidate.organiser_kind == Organiser.PARTY
    if not can_organise(user, None if party else candidate):
        raise _err("Not found.", "not_found", 404)
    if candidate is not None and not party and not candidate_published(candidate):
        raise _err("This candidate isn't published, so events can't be changed right now.", "candidate_not_published", 409)


def organiser_ids(event) -> set:
    """Accounts that can't register for or moderate this event (spec 18 §6.1, §8)."""
    from apps.moderation.targets import _linked

    if event.organiser_kind == Organiser.CAMPAIGN:
        return set(_linked(event.candidate_id))
    return {event.created_by_id} if event.created_by_id else set()


# ------------------------------------------------------------------ the law (rules 2–5)
def _blackout_for(event_or_candidate, at=None):
    candidate = event_or_candidate.candidate if isinstance(event_or_candidate, Event) else event_or_candidate
    if candidate is None:
        return None
    return blackouts.active_window(at=at, positions=[candidate.position])


def require_not_paused(candidate):
    w = _blackout_for(candidate)
    if w:
        raise _err(f"Campaign events are paused until {timezone.localtime(w['end']):%d %b %Y, %H:%M} (Electoral Act 2026 campaign blackout).",
                   "publishing_paused", 409, until=w["end"].isoformat())


def check_campaign_rules(candidate, type_, starts_at):
    """Rule 2: rallies and canvassing inside the office's campaign period; no campaign event starts in a blackout window."""
    if candidate is None:
        return
    if type_ in CAMPAIGNING_TYPES:
        p = blackouts.campaign_period(candidate.position)
        if p and not (p["start"] <= starts_at <= p["end"]):
            raise _err(f"Campaign rallies and canvassing for this office can only be held between {_when(p['start'])} and "
                       f"{_when(p['end'])} (Electoral Act 2026 s. 98).", "outside_campaign_period", 409,
                       period_start=p["start"].isoformat(), period_end=p["end"].isoformat())
    w = _blackout_for(candidate, at=starts_at)
    if w:
        raise _err(f"Campaign events can't start between {_when(w['start'])} and {_when(w['end'])} (campaign blackout).",
                   "publishing_paused", 409, until=w["end"].isoformat())


def needs_police_notice(event) -> bool:
    return event.type in PUBLIC_RALLY_TYPES and event.mode != Mode.ONLINE and event.venue_type not in (VenueType.PRIVATE_PREMISES, "")


def required_declarations(event) -> list[str]:
    out = ["no_inducements"]
    if event.mode != Mode.ONLINE:
        out.append("lawful_venue")
    if needs_police_notice(event):
        out.append("police_notified")
    return out


# ------------------------------------------------------------------ validation (§4.2)
FIELDS = ("type", "mode", "title", "description", "starts_at", "ends_at", "venue_name", "venue_address", "venue_type", "address_visibility",
          "state", "lga_text", "geo_lga", "online_url", "accessibility_notes", "capacity", "registration", "registration_closes_at",
          "members_only", "language")


def _clean(event, data, *, now):
    """Apply `data` onto `event` (unsaved) with §4.2's rules. Raises a ValidationError with field messages."""
    errors = {}
    for k in FIELDS:
        if k in data:
            setattr(event, k if k != "geo_lga" else "geo_lga_id", data[k])
    event.title = " ".join((event.title or "").split())
    event.description = clean_text(event.description)
    if "language" in data:
        try:
            event.language = clean_content_language(data["language"], "und")
        except serializers.ValidationError:
            errors["language"] = ["Choose English, Hausa, Igbo, Yoruba, Nigerian Pidgin or “Not sure”."]
    for k in ("venue_name", "venue_address", "lga_text", "accessibility_notes"):
        setattr(event, k, " ".join((getattr(event, k) or "").split()))
    if event.type not in EventType.values:
        errors["type"] = ["Choose what kind of event this is."]
    if event.mode not in Mode.values:
        errors["mode"] = ["Choose in person, online or both."]
    if not 10 <= len(event.title) <= 120:
        errors["title"] = ["Use 10 to 120 characters."]
    if not 30 <= len(event.description) <= 4000:
        errors["description"] = ["Use 30 to 4,000 characters."]
    if not event.starts_at or not event.ends_at:
        errors["starts_at"] = ["Choose when it starts and ends."]
    else:
        if event.ends_at <= event.starts_at:
            errors["ends_at"] = ["The end must be after the start."]
        elif event.ends_at - event.starts_at > timedelta(days=MAX_DAYS_LONG):
            errors["ends_at"] = ["An event can last at most 3 days."]
        if event.starts_at > now + MAX_AHEAD:
            errors["starts_at"] = ["Events can be at most a year ahead."]
    if event.mode == Mode.ONLINE:
        event.venue_name = event.venue_address = ""
        event.venue_type = VenueType.ONLINE
        event.address_visibility = AddressVisibility.PUBLIC
    else:
        if not 3 <= len(event.venue_name) <= 200:
            errors["venue_name"] = ["Give the venue's name (3 to 200 characters)."]
        if not 3 <= len(event.venue_address) <= 200:
            errors["venue_address"] = ["Give the address (3 to 200 characters)."]
        if event.venue_type not in VenueType.values or event.venue_type == VenueType.ONLINE:
            errors["venue_type"] = ["Choose the type of venue. Places of worship, police stations and public offices can't be used "
                                    "for campaigns (Electoral Act 2026 s. 96)."]
        if event.mode == Mode.IN_PERSON:
            event.online_url = ""
        if not event.state and event.organiser_kind:
            errors["state"] = ["Choose the state."]
    if event.mode != Mode.IN_PERSON:
        if not (event.online_url or "").lower().startswith("https://") or len(event.online_url) > 500:
            errors["online_url"] = ["Add the meeting link (it must start with https://)."]
    if event.address_visibility not in AddressVisibility.values:
        errors["address_visibility"] = ["Choose who can see the address."]
    if event.capacity is not None:
        try:
            event.capacity = int(event.capacity)
        except (TypeError, ValueError):
            errors["capacity"] = ["Use a whole number."]
        else:
            if not 1 <= event.capacity <= 100_000:
                errors["capacity"] = ["Use 1 to 100,000, or leave it empty for no limit."]
            elif event.pk and event.registered_count > event.capacity:
                raise _err("More people have registered than that. Close registration instead.", "capacity_below_registrations", 409)
    if event.registration not in Registration.values:
        errors["registration"] = ["Choose how people sign up."]
    if event.registration_closes_at and event.starts_at and event.registration_closes_at > event.starts_at:
        errors["registration_closes_at"] = ["Registration must close by the start."]
    if event.members_only and event.organiser_kind != Organiser.PARTY:
        errors["members_only"] = ["Only party events can be for NDC members only."]
    if len(event.accessibility_notes) > 300:
        errors["accessibility_notes"] = ["Use up to 300 characters."]
    if "geo_lga" not in data and ("lga_text" in data or "state" in data):
        # 16's geography: link a typed LGA name to the list (exact folded match), so LGA community strips find the event
        from apps.geo.matching import _key
        from apps.geo.models import Lga

        hit = (Lga.objects.filter(state__slug=event.state, folded_name=_key(event.lga_text), active=True).values_list("pk", flat=True).first()
               if event.state and event.lga_text else None)
        event.geo_lga_id = hit
    if event.geo_lga_id:
        from apps.geo.models import Lga

        chosen = bool(data.get("geo_lga"))  # spec 19 §7.2: a picker choice must be active; an old link may stay as it is
        lga = Lga.objects.filter(pk=event.geo_lga_id, **({"active": True} if chosen else {})).select_related("state").first()
        if lga is None or (event.state and lga.state.slug != event.state):
            errors["geo_lga"] = ["Choose an LGA in this state."]
        elif chosen or not event.lga_text:
            event.lga_text = lga.name[:100]
    if errors:
        raise serializers.ValidationError(errors)


def _history(event, kind, actor=None, note=""):
    EventChange.objects.create(event=event, kind=kind, actor=actor if _signed_in(actor) else None, note=(note or "")[:300])


def _locked(event) -> Event:
    return Event.objects.select_for_update(of=("self",)).select_related("candidate").get(pk=event.pk)


def create(user, candidate, data) -> Event:
    """A draft (§4.3). candidate=None → a party event (system admins)."""
    if candidate is None:
        if not can_organise(user, None):
            raise _err("Not found.", "not_found", 404)
    else:
        require_organiser(user, candidate)
        require_not_paused(candidate)
    now = timezone.now()
    e = Event(organiser_kind=Organiser.PARTY if candidate is None else Organiser.CAMPAIGN, candidate=candidate, created_by=user)
    _clean(e, data, now=now)
    check_campaign_rules(candidate, e.type, e.starts_at)
    with transaction.atomic():
        e.save()
        _history(e, ChangeKind.CREATED, user)
    return e


def edit(user, event, data) -> Event:
    """Drafts change freely; published events until they start. Date, time, venue, mode or link changes notify (§4.3)."""
    require_organiser(user, event)
    if event.candidate_id:
        require_not_paused(event.candidate)
    now = timezone.now()
    with transaction.atomic():
        e = _locked(event)
        if e.status not in (EventStatus.DRAFT, EventStatus.PUBLISHED):
            raise _err("Cancelled, hidden or removed events can't be changed.", "event_not_open", 409)
        if e.status == EventStatus.PUBLISHED and e.starts_at <= now:
            raise _err("This event has started, so it can't be changed.", "event_not_open", 409)
        before = {k: getattr(e, k) for k in MATERIAL_FIELDS}
        _clean(e, data, now=now)
        if e.status == EventStatus.PUBLISHED and e.starts_at <= now:
            raise serializers.ValidationError({"starts_at": ["Choose a time in the future."]})
        check_campaign_rules(e.candidate, e.type, e.starts_at)
        changed = [MATERIAL_FIELDS[k] for k in MATERIAL_FIELDS if getattr(e, k) != before[k]]
        e.save()
        if e.status == EventStatus.PUBLISHED and changed:
            kind = MaterialChange.TIME if MaterialChange.TIME in changed else changed[0]
            if kind == MaterialChange.TIME and e.starts_at > before["starts_at"] + timedelta(days=1):
                kind = MaterialChange.POSTPONED
            Event.objects.filter(pk=e.pk).update(material_change_at=now, material_change_kind=kind, reminder_day_sent_at=None,
                                                 reminder_hour_sent_at=None, flagged_blackout_at=None)
            _history(e, ChangeKind.MATERIAL_CHANGE, user, kind)
            _fanout(e, kind)
        else:
            _history(e, ChangeKind.EDITED, user)
        _reindex(e)
    return Event.objects.select_related("candidate").get(pk=e.pk)


def publish(user, event, declarations) -> Event:
    """Rules 3–5: the declarations are required and recorded with the time."""
    require_organiser(user, event)
    if event.candidate_id:
        require_not_paused(event.candidate)
    now = timezone.now()
    with transaction.atomic():
        e = _locked(event)
        if e.status != EventStatus.DRAFT:
            raise _err("Only drafts can be published.", "invalid_transition", 409)
        if e.starts_at <= now:
            raise serializers.ValidationError({"starts_at": ["Choose a time in the future."]})
        check_campaign_rules(e.candidate, e.type, e.starts_at)
        given = declarations if isinstance(declarations, dict) else {}
        missing = [k for k in required_declarations(e) if given.get(k) is not True]
        if missing:
            raise _err("Confirm every declaration before publishing.", "declaration_required", 400, missing=missing)
        e.declarations = {k: now.isoformat() for k in required_declarations(e)}
        e.status, e.published_at = EventStatus.PUBLISHED, now
        e.save(update_fields=["declarations", "status", "published_at", "updated_at"])
        _history(e, ChangeKind.PUBLISHED, user)
        _reindex(e)
        if e.organiser_kind == Organiser.CAMPAIGN:
            from apps.core import outbox

            outbox.enqueue("candidate_event_fanout", {"event": str(e.pk)}, dedupe_key=f"candidate_event:{e.pk}")
    record(AuditLog.Action.EVENT_PUBLISHED, target=e, actor=user, organiser=e.organiser_kind)
    if e.organiser_kind == Organiser.PARTY:
        record(AuditLog.Action.EVENT_PARTY_CREATED, target=e, actor=user)
    return e


def cancel(user, event, reason) -> Event:
    reason = " ".join((reason or "").split())
    if not 10 <= len(reason) <= 300:
        raise serializers.ValidationError({"reason": ["Say why, in 10 to 300 characters. Registrants see it."]})
    require_organiser(user, event)
    with transaction.atomic():
        e = _locked(event)
        if e.status != EventStatus.PUBLISHED:
            raise _err("Only published events can be cancelled.", "invalid_transition", 409)
        e.status, e.cancelled_at, e.cancel_reason = EventStatus.CANCELLED, timezone.now(), reason
        e.save(update_fields=["status", "cancelled_at", "cancel_reason", "updated_at"])
        _history(e, ChangeKind.CANCELLED, user, reason)
        _fanout(e, "cancelled")
        _reindex(e)
    record(AuditLog.Action.EVENT_CANCELLED, target=e, actor=user)
    return e


def delete_draft(user, event):
    require_organiser(user, event)
    with transaction.atomic():
        e = _locked(event)
        if e.status != EventStatus.DRAFT:
            raise _err("Published events can't be deleted, only cancelled.", "invalid_transition", 409)
        from apps.core.history import allow_history_delete

        with allow_history_delete():
            e.delete()


def close_registration(user, event) -> Event:
    require_organiser(user, event)
    with transaction.atomic():
        e = _locked(event)
        if e.registration != Registration.OPEN:
            return e
        e.registration = Registration.CLOSED
        e.save(update_fields=["registration", "updated_at"])
        _history(e, ChangeKind.REGISTRATION_CLOSED, user)
    return e


def add_update(user, event, text) -> EventUpdate:
    text = clean_text(text)
    if not 10 <= len(text) <= 500:
        raise serializers.ValidationError({"text": ["Use 10 to 500 characters."]})
    require_organiser(user, event)
    with transaction.atomic():
        e = _locked(event)
        if e.status != EventStatus.PUBLISHED or e.ends_at <= timezone.now():
            raise _err("Updates go to published events that haven't ended.", "event_not_open", 409)
        if e.updates.count() >= MAX_UPDATES:
            raise _err("An event can have at most 3 updates.", "update_limit", 409)
        u = EventUpdate.objects.create(event=e, text=text, created_by=user)
        _fanout(e, "update")
    return u


def set_image(user, event, upload, *, rights_confirmed) -> Event:
    from apps.media.services import asset_for_name
    from apps.news import images

    require_organiser(user, event)
    if not rights_confirmed:
        raise serializers.ValidationError({"rights_confirmed": ["Confirm you took this photo or have the right to share it."]})
    stored = images.store(upload, purpose="event_image", user=user)
    with transaction.atomic():
        e = _locked(event)
        if e.status not in (EventStatus.DRAFT, EventStatus.PUBLISHED):
            images.delete(stored["image"], stored["image_thumb"])
            raise _err("Cancelled, hidden or removed events can't be changed.", "event_not_open", 409)
        old = (e.cover_image, e.cover_image_thumb)
        e.cover_image, e.cover_image_thumb = stored["image"], stored["image_thumb"]
        e.cover_image_asset = asset_for_name(stored["image"])
        e.save(update_fields=["cover_image", "cover_image_thumb", "cover_image_asset", "updated_at"])
        _history(e, ChangeKind.IMAGE_CHANGED, user)
    images.delete(*[n for n in old if n])
    return e


def remove_image(user, event) -> Event:
    from apps.news import images

    require_organiser(user, event)
    with transaction.atomic():
        e = _locked(event)
        old = (e.cover_image, e.cover_image_thumb)
        e.cover_image = e.cover_image_thumb = ""
        e.cover_image_asset = None
        e.save(update_fields=["cover_image", "cover_image_thumb", "cover_image_asset", "updated_at"])
        _history(e, ChangeKind.IMAGE_CHANGED, user, "removed")
    images.delete(*[n for n in old if n])
    return e


# ------------------------------------------------------------------ who sees what
def is_public(event) -> bool:
    """Guests can open published and cancelled events whose organiser is listed (removed, hidden, draft: 404)."""
    if event.status not in LIVE:
        return False
    return event.organiser_kind == Organiser.PARTY or candidate_published(event.candidate)


def public_events():
    """Published or cancelled events of listed organisers (cancelled stay listed until their date, §4.3)."""
    return (Event.objects.filter(status__in=LIVE)
            .filter(Q(organiser_kind=Organiser.PARTY) | Q(candidate__status="published", candidate__verified_at__isnull=False))
            .select_related("candidate", "geo_lga"))


def upcoming(state=None, lga=None, candidate=None, limit=5, now=None):
    """The shared list for candidate profiles, state pages and community pages (§5)."""
    now = now or timezone.now()
    qs = public_events().filter(status=EventStatus.PUBLISHED, ends_at__gt=now)
    if candidate is not None:
        qs = qs.filter(candidate=candidate)
    if lga is not None:
        qs = qs.filter(geo_lga=lga)
    elif state:
        qs = qs.filter(state=state)
    return list(qs.order_by("starts_at", "id")[:limit])


def registration_for(user, event):
    if not _signed_in(user):
        return None
    return EventRegistration.objects.filter(event=event, user=user).exclude(status=RegistrationStatus.CANCELLED).first()


def sees_private_details(user, event, reg=None) -> bool:
    """The online link and a registered-only address: registered (not waitlisted) people and the organiser."""
    if not _signed_in(user):
        return False
    if user.pk in organiser_ids(event) or (event.organiser_kind == Organiser.PARTY and can_organise(user, None)):
        return True
    reg = reg if reg is not None else registration_for(user, event)
    return bool(reg and reg.status == RegistrationStatus.REGISTERED)


def band(n: int) -> str:
    return "Fewer than 10" if n < 10 else f"{n:,}"


# ------------------------------------------------------------------ registration (§6)
def registration_block(user, event, now=None):
    """Why this person can't register now: (code, message, status) or None."""
    now = now or timezone.now()
    if not _signed_in(user):
        return "not_authenticated", "Sign in to register.", 401
    if not user.profile_complete:
        return "profile_incomplete", "Finish your profile first.", 403
    if event.status != EventStatus.PUBLISHED or not is_public(event) or event.ends_at <= now:
        return "event_not_open", "This event isn't taking registrations.", 409
    if event.registration != Registration.OPEN or event.starts_at <= now or (event.registration_closes_at and event.registration_closes_at <= now):
        return "registration_closed", "Registration for this event has closed.", 409
    if user.pk in organiser_ids(event):
        return "permission_denied", "Organisers don't register for their own events.", 403
    if event.members_only:
        from apps.communities.services import _is_ndc_member

        if not _is_ndc_member(user):
            return "members_only", "This event is for NDC members only.", 403
    return None


def _ticket(event) -> str:
    taken = set(EventRegistration.objects.filter(event=event).values_list("ticket_code", flat=True))
    while True:
        code = "".join(secrets.choice(TICKET_ALPHABET) for _ in range(6))
        if code not in taken:
            return code


def _recount(event_id):
    regs = EventRegistration.objects.filter(event_id=event_id)
    Event.objects.filter(pk=event_id).update(
        registered_count=regs.filter(status=RegistrationStatus.REGISTERED).count(),
        waitlist_count=regs.filter(status=RegistrationStatus.WAITLISTED).count(),
        checked_in_count=regs.filter(checked_in_at__isnull=False).count())


def register(user, event) -> EventRegistration:
    from apps.privacy.consent import require_consent

    b = registration_block(user, event)
    if b:
        raise _err(b[1], b[0], b[2])
    require_consent(user, PURPOSE)
    with transaction.atomic():
        e = Event.objects.select_for_update().get(pk=event.pk)  # §18: two people, the last place → one registered, one waitlisted
        b = registration_block(user, e)
        if b:
            raise _err(b[1], b[0], b[2])
        now = timezone.now()
        upcoming_n = EventRegistration.objects.filter(user=user, event__ends_at__gt=now).exclude(status=RegistrationStatus.CANCELLED) \
            .exclude(event=e).count()
        if upcoming_n >= MAX_UPCOMING_PER_PERSON:
            raise _err("You can be registered for at most 20 upcoming events.", "too_many_registrations", 409)
        reg = EventRegistration.objects.select_for_update().filter(event=e, user=user).first()
        if reg and reg.status != RegistrationStatus.CANCELLED:
            raise _err("You're already registered.", "already_registered", 409)
        if reg and reg.cancelled_count >= MAX_REREGISTER:
            raise _err("You've cancelled this event too many times to register again.", "too_many_registrations", 409)
        full = e.capacity is not None and e.registered_count >= e.capacity
        if full and e.waitlist_count >= min(e.capacity, WAITLIST_CAP):
            raise _err("This event and its waitlist are full.", "registration_closed", 409)
        if full:
            last = EventRegistration.objects.filter(event=e, status=RegistrationStatus.WAITLISTED).order_by("-waitlist_position") \
                .values_list("waitlist_position", flat=True).first() or 0
            status, position = RegistrationStatus.WAITLISTED, last + 1
        else:
            status, position = RegistrationStatus.REGISTERED, None
        if reg is None:
            reg = EventRegistration.objects.create(event=e, user=user, status=status, waitlist_position=position, ticket_code=_ticket(e))
        else:
            reg.status, reg.waitlist_position, reg.checked_in_at = status, position, None
            reg.save(update_fields=["status", "waitlist_position", "checked_in_at", "updated_at"])
        _recount(e.pk)
    return reg


def _promote(e):
    """Fill free places from the waitlist in order; each promoted person is told (event_waitlist)."""
    from apps.notifications.services import notify

    e.refresh_from_db(fields=["registered_count", "capacity", "status", "starts_at"])
    if e.status != EventStatus.PUBLISHED or e.starts_at <= timezone.now():
        return 0
    free = (e.capacity - e.registered_count) if e.capacity is not None else 10**9
    n = 0
    for reg in EventRegistration.objects.select_for_update().filter(event=e, status=RegistrationStatus.WAITLISTED) \
            .order_by("waitlist_position").select_related("user")[:max(free, 0)]:
        reg.status, reg.waitlist_position = RegistrationStatus.REGISTERED, None
        reg.save(update_fields=["status", "waitlist_position", "updated_at"])
        notify(reg.user, "event_waitlist", target=e)
        n += 1
    if n:
        _recount(e.pk)
    return n


def cancel_registration(user, event) -> EventRegistration:
    with transaction.atomic():
        e = Event.objects.select_for_update().get(pk=event.pk)
        reg = EventRegistration.objects.select_for_update().filter(event=e, user=user).exclude(status=RegistrationStatus.CANCELLED).first()
        if reg is None:
            raise _err("You're not registered for this event.", "not_found", 404)
        if e.starts_at <= timezone.now():
            raise _err("This event has started.", "event_not_open", 409)
        was = reg.status
        reg.status, reg.waitlist_position, reg.cancelled_count = RegistrationStatus.CANCELLED, None, reg.cancelled_count + 1
        reg.save(update_fields=["status", "waitlist_position", "cancelled_count", "updated_at"])
        _recount(e.pk)
        if was == RegistrationStatus.REGISTERED:
            _promote(e)
    return reg


def cancel_upcoming_for(user) -> int:
    """Consent withdrawn (§17.1): upcoming registrations are deleted; their places go to the waitlist."""
    now = timezone.now()
    n = 0
    with transaction.atomic():
        for reg in list(EventRegistration.objects.select_for_update().filter(user=user, event__ends_at__gt=now)):
            was, event_id = reg.status, reg.event_id
            reg.delete()
            _recount(event_id)
            if was == RegistrationStatus.REGISTERED:
                _promote(Event.objects.select_for_update().get(pk=event_id))
            n += 1
    return n


# ------------------------------------------------------------------ check-in (§6.4)
def checkin_open(event, now=None) -> bool:
    now = now or timezone.now()
    return event.starts_at - CHECKIN_BEFORE <= now <= event.ends_at + CHECKIN_AFTER


def checkin_list(user, event, request=None):
    from apps.communities.text import short_name

    require_organiser(user, event)
    if event.status not in (EventStatus.PUBLISHED,) or not checkin_open(event):
        raise _err("The check-in list opens 24 hours before the event and closes 24 hours after it.", "checkin_window_closed", 409)
    _history(event, ChangeKind.CHECKIN_LIST_OPENED, user)
    record(AuditLog.Action.EVENT_CHECKIN_LIST_OPENED, target=event, actor=user, request=request)
    regs = EventRegistration.objects.filter(event=event, status=RegistrationStatus.REGISTERED).select_related("user").order_by("ticket_code")
    return [{"ticket_code": r.ticket_code, "name": short_name(r.user), "checked_in": r.checked_in_at is not None} for r in regs]


def check_in(user, event, code) -> dict:
    from apps.communities.text import short_name

    require_organiser(user, event)
    if event.status != EventStatus.PUBLISHED or not checkin_open(event):
        raise _err("The check-in list opens 24 hours before the event and closes 24 hours after it.", "checkin_window_closed", 409)
    code = "".join(str(code or "").upper().split())
    with transaction.atomic():
        reg = EventRegistration.objects.select_for_update().filter(event=event, ticket_code=code).select_related("user").first()
        if reg is None or reg.status != RegistrationStatus.REGISTERED:
            raise _err("No registration with that code.", "not_found", 404)
        if reg.checked_in_at is None:
            reg.checked_in_at = timezone.now()
            reg.save(update_fields=["checked_in_at", "updated_at"])
            _recount(event.pk)
    return {"ticket_code": reg.ticket_code, "name": short_name(reg.user), "checked_in": True}


# ------------------------------------------------------------------ moderation (13; called by moderation only)
def hide(event, moderator, note=""):
    if event.status != EventStatus.PUBLISHED:
        raise _err("Only published events can be hidden.", "invalid_transition", 409)
    Event.objects.filter(pk=event.pk).update(status=EventStatus.HIDDEN, hidden_at=timezone.now())
    _history(event, ChangeKind.HIDDEN, moderator, note)
    _reindex(Event.objects.get(pk=event.pk))


def unhide(event, moderator, note=""):
    if event.status != EventStatus.HIDDEN:
        raise _err("This event isn't hidden.", "invalid_transition", 409)
    Event.objects.filter(pk=event.pk).update(status=EventStatus.PUBLISHED)
    _history(event, ChangeKind.UNHIDDEN, moderator, note)
    _reindex(Event.objects.get(pk=event.pk))


def remove(event, moderator, *, rule, note_to_author="", internal_note="", source=None):
    from apps.moderation.services import content_removed

    if event.status not in (EventStatus.PUBLISHED, EventStatus.HIDDEN, EventStatus.CANCELLED):
        raise _err("Only visible or hidden events can be removed.", "invalid_transition", 409)
    Event.objects.filter(pk=event.pk).update(status=EventStatus.REMOVED, removed_at=timezone.now(), removed_rule=rule)
    e = Event.objects.select_related("candidate").get(pk=event.pk)
    _history(e, ChangeKind.REMOVED, moderator, rule)
    _fanout(e, "removed")
    _reindex(e)
    return content_removed("event", e, moderator, rule_id=rule, note_to_author=note_to_author, internal_note=internal_note, source=source)


def restore(event, moderator, note=""):
    from apps.moderation.services import content_restored

    if event.status != EventStatus.REMOVED:
        raise _err("Only removed events can be restored.", "invalid_transition", 409)
    back = EventStatus.CANCELLED if event.cancelled_at else EventStatus.PUBLISHED
    Event.objects.filter(pk=event.pk).update(status=back, removed_at=None, removed_rule="")
    e = Event.objects.select_related("candidate").get(pk=event.pk)
    _history(e, ChangeKind.RESTORED, moderator, note)
    _reindex(e)
    return content_restored("event", e, moderator, note)


# ------------------------------------------------------------------ notifications and search
def _fanout(event, decision):
    from apps.core import outbox

    outbox.enqueue("events_fanout", {"event": str(event.pk), "decision": decision},
                   dedupe_key=f"events_fanout:{event.pk}:{decision}:{timezone.now():%Y%m%d%H%M%S%f}")


def _reindex(event):
    try:
        from apps.search import index
    except ImportError:  # pragma: no cover
        return
    index.sync_event(event)


def end_for_candidate(candidate):
    """The candidate is unpublished or archived (§18): their upcoming events stop taking registrations."""
    now = timezone.now()
    n = Event.objects.filter(candidate=candidate, status=EventStatus.PUBLISHED, ends_at__gt=now, registration=Registration.OPEN) \
        .update(registration=Registration.CLOSED)
    for e in Event.objects.filter(candidate=candidate, ends_at__gt=now):
        _reindex(e)
    return n


# ------------------------------------------------------------------ jobs (§12.3)
def tick(now=None) -> dict:
    """Reminders, deadlines, and campaign events that a newly added blackout window now covers (flagged, never cancelled)."""
    from apps.core import outbox

    now = now or timezone.now()
    out = {"reminders_day": 0, "reminders_hour": 0, "closed": 0, "flagged": 0}
    base = Event.objects.filter(status=EventStatus.PUBLISHED)
    for e in base.filter(reminder_day_sent_at__isnull=True, starts_at__gt=now, starts_at__lte=now + timedelta(hours=24)):
        Event.objects.filter(pk=e.pk).update(reminder_day_sent_at=now)
        if e.starts_at - now >= timedelta(hours=22):  # §18: more than 2 hours late is skipped, not sent late
            outbox.enqueue("events_reminders", {"event": str(e.pk), "which": "day"}, dedupe_key=f"reminder:day:{e.pk}:{e.starts_at:%s}")
            out["reminders_day"] += 1
    for e in base.filter(reminder_hour_sent_at__isnull=True, mode__in=[Mode.ONLINE, Mode.HYBRID], starts_at__gt=now,
                         starts_at__lte=now + timedelta(hours=1)):
        Event.objects.filter(pk=e.pk).update(reminder_hour_sent_at=now)
        outbox.enqueue("events_reminders", {"event": str(e.pk), "which": "hour"}, dedupe_key=f"reminder:hour:{e.pk}:{e.starts_at:%s}")
        out["reminders_hour"] += 1
    out["closed"] = base.filter(registration=Registration.OPEN).filter(
        Q(starts_at__lte=now) | Q(registration_closes_at__lte=now)).update(registration=Registration.CLOSED)
    for e in base.filter(organiser_kind=Organiser.CAMPAIGN, flagged_blackout_at__isnull=True, starts_at__gt=now).select_related("candidate"):
        if _blackout_for(e, at=e.starts_at):
            Event.objects.filter(pk=e.pk).update(flagged_blackout_at=now)
            _fanout_organisers_blackout(e)
            out["flagged"] += 1
    return out


def _fanout_organisers_blackout(e):
    from apps.accounts.models import User
    from apps.notifications.services import notify

    for u in User.objects.filter(pk__in=organiser_ids(e)):
        notify(u, "event_update", target=e, decision="blackout")


def stats(now=None) -> dict:
    now = now or timezone.now()
    return {"upcoming": public_events().filter(status=EventStatus.PUBLISHED, ends_at__gt=now).count(),
            "this_week": public_events().filter(status=EventStatus.PUBLISHED, starts_at__gte=now, starts_at__lt=now + timedelta(days=7)).count(),
            "blackout_flagged": Event.objects.filter(status=EventStatus.PUBLISHED, flagged_blackout_at__isnull=False, starts_at__gt=now).count()}


# ------------------------------------------------------------------ privacy (§17)
def erase_for_deleted_user(user) -> dict:
    """Account deletion: registrations deleted (places go to the waitlist); events they created keep created_by empty."""
    ids = list(EventRegistration.objects.filter(user=user).values_list("event_id", flat=True))
    regs = cancel_upcoming_for(user)
    n, _ = EventRegistration.objects.filter(user=user).delete()
    for eid in set(ids):
        _recount(eid)
    Event.objects.filter(created_by=user).update(created_by=None)
    return {"registrations": n + regs}


def prune(now=None) -> dict:
    """§17.3: registrations 90 days after the end; events 2 years after the end (not under legal hold or an open case)."""
    from apps.core.history import allow_history_delete
    from apps.moderation.models import Case, CaseStatus

    now = now or timezone.now()
    regs, _ = EventRegistration.objects.filter(event__ends_at__lt=now - timedelta(days=90)).delete()
    held = set(Case.objects.filter(target_kind="event").filter(Q(legal_hold=True) | ~Q(status=CaseStatus.CLOSED))
               .values_list("target_id", flat=True))
    old = Event.objects.filter(ends_at__lt=now - timedelta(days=730)).exclude(pk__in=held)
    names = [n for row in old.values_list("cover_image", "cover_image_thumb") for n in row if n]
    with transaction.atomic(), allow_history_delete():
        events = old.count()
        old.delete()  # registrations, updates and history go with their event
    if names:
        from apps.news import images

        images.delete(*names)
    return {"registrations": regs, "events": events}


def counts_by_status(event) -> dict:
    return {"registered": event.registered_count, "waitlisted": event.waitlist_count, "checked_in": event.checked_in_count,
            "cancelled": EventRegistration.objects.filter(event=event, status=RegistrationStatus.CANCELLED).count()}

