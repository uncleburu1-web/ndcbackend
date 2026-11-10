"""Events API shapes (spec 18 §13). Payload builders below; the serializer classes document them for OpenAPI."""
from datetime import timezone as dt_tz

from django.utils import timezone
from rest_framework import serializers as S

from apps.media.services import srcset
from apps.news import images

from . import services
from .models import (AddressVisibility, EventStatus, EventType, MaterialChange, Mode, Organiser, Registration, RegistrationStatus,
                     VenueType)


# ------------------------------------------------------------------ OpenAPI shapes
class OrganiserCandidateSchema(S.Serializer):
    id = S.UUIDField()
    slug = S.CharField()
    full_name = S.CharField()
    position_label = S.CharField()


class OrganiserSchema(S.Serializer):
    kind = S.ChoiceField(Organiser.choices)
    label = S.CharField(help_text="'NDC' or '<candidate>'s campaign'")
    candidate = OrganiserCandidateSchema(allow_null=True)


class ImageSchema(S.Serializer):
    url = S.CharField()
    url_thumb = S.CharField()
    srcset = S.CharField(allow_null=True)


class MaterialChangeSchema(S.Serializer):
    at = S.DateTimeField()
    kind = S.ChoiceField(MaterialChange.choices)
    words = S.CharField()


class RemovedSchema(S.Serializer):
    rule = S.CharField()
    rule_title = S.CharField()


class UpdateSchema(S.Serializer):
    text = S.CharField()
    created_at = S.DateTimeField()


class RegisterBlockSchema(S.Serializer):
    code = S.CharField()
    message = S.CharField()


class EventCardSchema(S.Serializer):
    id = S.UUIDField()
    title = S.CharField()
    type = S.ChoiceField(EventType.choices)
    type_label = S.CharField()
    mode = S.ChoiceField(Mode.choices)
    mode_label = S.CharField()
    starts_at = S.DateTimeField()
    ends_at = S.DateTimeField()
    state = S.CharField()
    state_label = S.CharField()
    lga = S.CharField()
    place = S.CharField(help_text="Venue name, 'Online', or 'Address shown when you register'")
    organiser = OrganiserSchema()
    status = S.ChoiceField(EventStatus.choices)
    going = S.CharField(help_text="Banded below 10 (spec 18 rule 7)")
    full = S.BooleanField()
    registration = S.ChoiceField(Registration.choices)
    cover = ImageSchema(allow_null=True)
    ended = S.BooleanField()


class EventDetailSchema(EventCardSchema):
    description = S.CharField()
    venue_name = S.CharField()
    venue_address = S.CharField(allow_null=True, help_text="Null when only registered people may see it")
    venue_type = S.CharField()
    venue_type_label = S.CharField()
    address_visibility = S.ChoiceField(AddressVisibility.choices)
    online_url = S.CharField(allow_null=True, help_text="Only for registered people and the organiser")
    accessibility_notes = S.CharField()
    capacity = S.IntegerField(allow_null=True)
    places_left = S.IntegerField(allow_null=True)
    registration_closes_at = S.DateTimeField(allow_null=True)
    members_only = S.BooleanField()
    cancelled_reason = S.CharField()
    material_change = MaterialChangeSchema(allow_null=True)
    removed = RemovedSchema(allow_null=True)
    updates = UpdateSchema(many=True, help_text="Organiser updates; only for registered people and the organiser")
    can_register = S.BooleanField()
    register_block = RegisterBlockSchema(allow_null=True)
    calendar_url = S.CharField()


class MyRegistrationSchema(S.Serializer):
    status = S.ChoiceField(RegistrationStatus.choices)
    ticket_code = S.CharField()
    waitlist_position = S.IntegerField(allow_null=True)
    checked_in = S.BooleanField()


class EventMeSchema(S.Serializer):
    registration = MyRegistrationSchema(allow_null=True)
    is_organiser = S.BooleanField()
    online_url = S.CharField(allow_null=True)
    venue_address = S.CharField(allow_null=True)
    updates = UpdateSchema(many=True)
    can_register = S.BooleanField(required=False)
    register_block = RegisterBlockSchema(allow_null=True, required=False)


class MyEventSchema(S.Serializer):
    event = EventCardSchema()
    registration = MyRegistrationSchema()
    online_url = S.CharField(allow_null=True)


class CountsSchema(S.Serializer):
    registered = S.IntegerField()
    waitlisted = S.IntegerField()
    checked_in = S.IntegerField()
    cancelled = S.IntegerField()


class OrganiserEventSchema(EventDetailSchema):
    counts = CountsSchema()
    declarations_needed = S.ListField(child=S.CharField())
    declarations = S.DictField()
    checkin_open = S.BooleanField()
    blackout_flagged = S.BooleanField()
    geo_lga = S.CharField(allow_null=True, help_text="The linked LGA's ID as text (18 published it as a string; spec 19 keeps that)")
    online_url_value = S.CharField()
    venue_address_value = S.CharField()


class EventWriteSchema(S.Serializer):
    type = S.ChoiceField(EventType.choices)
    mode = S.ChoiceField(Mode.choices)
    title = S.CharField(max_length=120)
    description = S.CharField(max_length=4000)
    starts_at = S.DateTimeField()
    ends_at = S.DateTimeField()
    venue_name = S.CharField(required=False, allow_blank=True)
    venue_address = S.CharField(required=False, allow_blank=True)
    venue_type = S.ChoiceField(VenueType.choices, required=False, allow_blank=True)
    address_visibility = S.ChoiceField(AddressVisibility.choices, required=False)
    state = S.CharField(required=False, allow_blank=True)
    lga_text = S.CharField(required=False, allow_blank=True)
    geo_lga = S.IntegerField(required=False, allow_null=True, help_text="The LGA picker's choice (spec 19 §7.2); lga_text is written from it")
    online_url = S.CharField(required=False, allow_blank=True)
    accessibility_notes = S.CharField(required=False, allow_blank=True)
    capacity = S.IntegerField(required=False, allow_null=True)
    registration = S.ChoiceField(Registration.choices, required=False)
    registration_closes_at = S.DateTimeField(required=False, allow_null=True)
    members_only = S.BooleanField(required=False)


class EventPatchSchema(EventWriteSchema):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        for f in self.fields.values():
            f.required = False


class DeclarationsSchema(S.Serializer):
    declarations = S.DictField(child=S.BooleanField(), help_text="{no_inducements, lawful_venue, police_notified}: true for each needed")


# ------------------------------------------------------------------ payloads
def _organiser(e):
    c = e.candidate
    return {"kind": e.organiser_kind, "label": e.organiser_label,
            "candidate": {"id": str(c.pk), "slug": c.slug, "full_name": c.full_name, "position_label": c.get_position_display()} if c else None}


def _cover(e, request=None):
    if not e.cover_image:
        return None
    return {"url": images.url(e.cover_image, request), "url_thumb": images.url(e.cover_image_thumb or e.cover_image, request),
            "srcset": srcset(e.cover_image, "content", request, small=e.cover_image_thumb)}


def _place(e, private=False):
    if e.mode == Mode.ONLINE:
        return "Online"
    if e.address_visibility == AddressVisibility.REGISTERED and not private:
        return "Address shown when you register"
    return e.venue_name


def card(e, request=None) -> dict:
    now = timezone.now()
    return {"id": str(e.pk), "title": e.title, "type": e.type, "type_label": e.get_type_display(), "mode": e.mode,
            "mode_label": e.get_mode_display(), "starts_at": e.starts_at, "ends_at": e.ends_at, "state": e.state,
            "state_label": e.get_state_display() if e.state else "Nationwide", "lga": e.lga_text, "place": _place(e),
            "organiser": _organiser(e), "status": e.status, "going": services.band(e.registered_count),
            "full": e.capacity is not None and e.registered_count >= e.capacity, "registration": e.registration,
            "cover": _cover(e, request), "ended": e.ends_at <= now}


def _material(e):
    if not e.material_change_at:
        return None
    return {"at": e.material_change_at, "kind": e.material_change_kind, "words": MaterialChange(e.material_change_kind).label}


def _removed(e):
    if e.status != EventStatus.REMOVED:
        return None
    from apps.moderation.rules import rule_title

    return {"rule": e.removed_rule, "rule_title": rule_title(e.removed_rule)}


def _updates(e):
    return [{"text": u.text, "created_at": u.created_at} for u in e.updates.all()]


def detail(e, user, request=None) -> dict:
    """Public detail: per-user parts (registration, private address, link, updates) only come from /events/{id}/me/."""
    out = card(e, request)
    if e.status == EventStatus.REMOVED:
        return {**out, **{k: "" for k in ("description", "venue_name", "venue_type", "venue_type_label", "accessibility_notes",
                                           "cancelled_reason")},
                "venue_address": None, "address_visibility": e.address_visibility, "online_url": None, "capacity": None,
                "places_left": None, "registration_closes_at": None, "members_only": False, "material_change": None,
                "removed": _removed(e), "updates": [], "can_register": False, "register_block": None, "calendar_url": ""}
    block = services.registration_block(user, e) if e.registration != Registration.NONE else None
    left = max(e.capacity - e.registered_count, 0) if e.capacity is not None else None
    out.update({
        "description": e.description, "venue_name": e.venue_name if e.mode != Mode.ONLINE else "",
        "venue_address": e.venue_address if e.address_visibility == AddressVisibility.PUBLIC and e.mode != Mode.ONLINE else None,
        "venue_type": e.venue_type, "venue_type_label": e.get_venue_type_display() if e.venue_type else "",
        "address_visibility": e.address_visibility, "online_url": None, "accessibility_notes": e.accessibility_notes,
        "capacity": e.capacity, "places_left": left, "registration_closes_at": e.registration_closes_at, "members_only": e.members_only,
        "cancelled_reason": e.cancel_reason if e.status == EventStatus.CANCELLED else "", "material_change": _material(e),
        "removed": None, "updates": [], "can_register": block is None and e.registration == Registration.OPEN,
        "register_block": {"code": block[0], "message": block[1]} if block and block[0] != "not_authenticated" else None,
        "calendar_url": f"/api/v1/events/{e.pk}/calendar.ics",
    })
    return out


def registration(reg) -> dict | None:
    if reg is None:
        return None
    return {"status": reg.status, "ticket_code": reg.ticket_code, "waitlist_position": reg.waitlist_position,
            "checked_in": reg.checked_in_at is not None}


def me(e, user) -> dict:
    reg = services.registration_for(user, e)
    private = services.sees_private_details(user, e, reg)
    organiser = services._signed_in(user) and (user.pk in services.organiser_ids(e)
                                                or (e.organiser_kind == Organiser.PARTY and services.can_organise(user, None)))
    return {"registration": registration(reg), "is_organiser": bool(organiser),
            "online_url": e.online_url or None if private and e.mode != Mode.IN_PERSON else None,
            "venue_address": e.venue_address if private and e.mode != Mode.ONLINE else None,
            "updates": _updates(e) if (private or (reg is not None)) else []}


def organiser_view(e, request=None) -> dict:
    out = detail(e, None, request)
    out.update({"venue_address": e.venue_address or None, "online_url": e.online_url or None, "updates": _updates(e),
                "counts": services.counts_by_status(e), "declarations_needed": services.required_declarations(e),
                "declarations": e.declarations or {}, "checkin_open": services.checkin_open(e) and e.status == EventStatus.PUBLISHED,
                "blackout_flagged": e.flagged_blackout_at is not None, "geo_lga": str(e.geo_lga_id) if e.geo_lga_id else None,
                "online_url_value": e.online_url, "venue_address_value": e.venue_address, "lga_text": e.lga_text,
                "status": e.status})
    return out


def ics(e) -> str:
    """RFC 5545 calendar file: title, times, the event URL; the venue only when public; never the online link (§5)."""
    from django.conf import settings

    def esc(v):
        return str(v).replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

    fmt = "%Y%m%dT%H%M%SZ"
    url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/events/{e.pk}"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//NDCConnect//Events//EN", "CALSCALE:GREGORIAN", "BEGIN:VEVENT",
             f"UID:{e.pk}@ndcconnect", f"DTSTAMP:{timezone.now().astimezone(dt_tz.utc):{fmt}}",
             f"DTSTART:{e.starts_at.astimezone(dt_tz.utc):{fmt}}", f"DTEND:{e.ends_at.astimezone(dt_tz.utc):{fmt}}",
             f"SUMMARY:{esc(e.title)}", f"DESCRIPTION:{esc(e.organiser_label + ' · ' + url)}", f"URL:{url}"]
    if e.mode != Mode.ONLINE and e.address_visibility == AddressVisibility.PUBLIC:
        lines.append(f"LOCATION:{esc(e.venue_name + ', ' + e.venue_address)}")
    elif e.mode == Mode.ONLINE:
        lines.append("LOCATION:Online")
    if e.status == EventStatus.CANCELLED:
        lines.append("STATUS:CANCELLED")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"

