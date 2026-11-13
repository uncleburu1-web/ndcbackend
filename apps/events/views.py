"""Events API — spec 18 §13. Public lists and pages are cacheable (no per-user data); per-user parts come from /me endpoints."""
import uuid

from django.http import HttpResponse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers as DS
from rest_framework import status
from rest_framework.pagination import CursorPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.audit.services import client_ip
from apps.candidates.models import Candidate
from apps.candidates.photos import PhotoError
from apps.core.exceptions import ServiceError
from apps.news.serializers import ReportSerializer
from apps.news.views import PublicThrottle, ReportThrottle

from . import serializers as P
from . import services
from .models import (
    Event,
    EventRegistration,
    EventStatus,
    Mode,
    Organiser,
    RegistrationStatus,
)


def _throttle(scope_name, methods=("POST", "PATCH", "PUT", "DELETE")):
    class _T(UserRateThrottle):
        scope = scope_name

        def allow_request(self, request, view):
            return True if request.method not in methods else super().allow_request(request, view)

    _T.__name__ = f"Throttle_{scope_name}"
    return _T


WriteThrottle = _throttle("event_write")
RegisterThrottle = _throttle("event_register", ("POST", "DELETE"))
UpdateThrottle = _throttle("campaign_update", ("POST",))


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class _Public(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []  # no session read: the same answer for everyone, cacheable (spec 15 rule)
    throttle_classes = [PublicThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if response.status_code == 200:
            patch_cache_control(response, public=True, max_age=60)
        return response


def _uuid(v):
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise ServiceError("Not found.", code="not_found", status_code=404) from None


def _public_event(pk) -> Event:
    e = Event.objects.select_related("candidate", "geo_lga").filter(pk=_uuid(pk)).first()
    if e is None or not (services.is_public(e) or e.status == EventStatus.REMOVED and e.published_at):
        raise ServiceError("Not found.", code="not_found", status_code=404)
    return e


class EventPage(CursorPagination):
    page_size = 20
    ordering = ("starts_at", "id")


class PastPage(CursorPagination):
    page_size = 20
    ordering = ("-starts_at", "-id")


def _filtered(qp, now):
    qs = services.public_events()
    if qp.get("when") == "past":
        qs = qs.filter(ends_at__lte=now, ends_at__gt=now - services.PAST_VISIBLE)
    else:
        qs = qs.filter(ends_at__gt=now)
    if qp.get("state"):
        qs = qs.filter(state=qp["state"])
    if qp.get("lga"):  # spec 19 §6.2: geography IDs are integers
        lga = str(qp["lga"]).strip()
        qs = qs.filter(geo_lga_id=int(lga)) if lga.isdigit() else qs.none()
    if qp.get("type"):
        qs = qs.filter(type=qp["type"])
    if qp.get("mode") == "online":
        qs = qs.filter(mode__in=[Mode.ONLINE, Mode.HYBRID])
    elif qp.get("mode") in Mode.values:
        qs = qs.filter(mode=qp["mode"])
    if qp.get("organiser") == "party":
        qs = qs.filter(organiser_kind=Organiser.PARTY)
    if qp.get("candidate"):
        qs = qs.filter(candidate__slug=qp["candidate"])
    if qp.get("community"):  # spec 18 §5: a state community shows its state's events; LGA and below, their LGA's
        from apps.communities.models import Community

        c = Community.objects.select_related("parent__parent").filter(pk=_uuid(qp["community"])).first()
        lga = None if c is None else c.geo_lga_id or (c.parent.geo_lga_id if c.parent and c.parent.geo_lga_id else None) or (
            c.parent.parent.geo_lga_id if c.parent and c.parent.parent else None)
        if c is None or c.level == "national":
            qs = qs.none() if c is None else qs
        elif c.level == "state":
            qs = qs.filter(state=c.state)
        else:
            qs = qs.filter(geo_lga_id=lga) if lga else qs.filter(state=c.state)
    return qs


EVENT_LIST = inline_serializer("EventList", {"results": P.EventCardSchema(many=True), "next": DS.CharField(allow_null=True),
                                              "previous": DS.CharField(allow_null=True)})


class EventList(_Public):
    @extend_schema(summary="Upcoming (or past) events, soonest first, 20 a page (spec 18 §5)", responses=EVENT_LIST,
                   parameters=[OpenApiParameter(n, str) for n in ("state", "lga", "type", "mode", "organiser", "candidate", "community", "when",
                                                                  "this_week")])
    def get(self, request):
        from datetime import timedelta

        now = timezone.now()
        qp = request.query_params
        qs = _filtered(qp, now)
        if qp.get("this_week") in ("1", "true"):
            qs = qs.filter(starts_at__lt=now + timedelta(days=7))
        paginator = PastPage() if qp.get("when") == "past" else EventPage()
        page = paginator.paginate_queryset(qs, request, view=self)
        return Response({"results": [P.card(e, request) for e in page], "next": paginator.get_next_link(),
                         "previous": paginator.get_previous_link()})


class EventDetail(_Public):
    @extend_schema(summary="One event; the online link and a private address only via /events/{id}/me/", responses=P.EventDetailSchema)
    def get(self, request, pk):
        return Response(P.detail(_public_event(pk), None, request))


class EventCalendar(_Public):
    @extend_schema(summary="Calendar file (.ics): never the online link", responses=OpenApiResponse(OpenApiTypes.STR))
    def get(self, request, pk):
        e = _public_event(pk)
        if e.status == EventStatus.REMOVED:
            raise ServiceError("Not found.", code="not_found", status_code=404)
        resp = HttpResponse(P.ics(e), content_type="text/calendar; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="event-{e.pk}.ics"'
        return resp


class EventMe(_Private):
    permission_classes = [AllowAny]

    @extend_schema(summary="Your registration, and the online link or private address if you're registered", responses=P.EventMeSchema)
    def get(self, request, pk):
        e = _public_event(pk)
        out = P.me(e, request.user)
        b = services.registration_block(request.user, e)
        out["can_register"] = b is None and e.registration == "open"
        out["register_block"] = {"code": b[0], "message": b[1]} if b else None
        return Response(out)


class EventRegistrationView(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [RegisterThrottle]

    @extend_schema(summary="Register (or join the waitlist); consent `event_registration` first", request=None,
                   responses={201: P.EventMeSchema})
    def post(self, request, pk):
        e = _public_event(pk)
        services.register(request.user, e)
        e.refresh_from_db()
        return Response(P.me(e, request.user), status=status.HTTP_201_CREATED)

    @extend_schema(summary="Cancel your registration", responses=P.EventMeSchema)
    def delete(self, request, pk):
        e = _public_event(pk)
        services.cancel_registration(request.user, e)
        e.refresh_from_db()
        return Response(P.me(e, request.user))


class EventReport(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [ReportThrottle]

    @extend_schema(summary="Report an event (13's reasons plus `inducement` and `banned_venue`)", request=ReportSerializer,
                   responses={201: inline_serializer("EventReportResponse", {"reference": DS.CharField(required=False),
                                                                             "route": DS.CharField(required=False)})})
    def post(self, request, pk):
        from apps.moderation import services as moderation

        e = _public_event(pk)
        ser = ReportSerializer(data=request.data, context={"kind": "event"})
        ser.is_valid(raise_exception=True)
        user = request.user if request.user and request.user.is_authenticated else None
        out = moderation.report("event", e, ser.validated_data["reason"], ser.validated_data.get("details", ""), user=user,
                                contact_email=ser.validated_data.get("contact_email", ""), ip=client_ip(request) or "")
        return Response(out, status=status.HTTP_200_OK if "route" in out else status.HTTP_201_CREATED)


MY_EVENTS = inline_serializer("MyEvents", {"results": P.MyEventSchema(many=True)})


class MyEvents(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Your upcoming (or past, 90 days) registrations", responses=MY_EVENTS,
                   parameters=[OpenApiParameter("when", str, enum=["upcoming", "past"])])
    def get(self, request):
        now = timezone.now()
        qs = EventRegistration.objects.filter(user=request.user).exclude(status=RegistrationStatus.CANCELLED) \
            .select_related("event__candidate").order_by("event__starts_at")
        if request.query_params.get("when") == "past":
            qs = qs.filter(event__ends_at__lte=now, event__ends_at__gt=now - services.PAST_VISIBLE).order_by("-event__starts_at")
        else:
            qs = qs.filter(event__ends_at__gt=now)
        out = []
        for r in qs:
            e = r.event
            if e.status == EventStatus.DRAFT:
                continue
            private = r.status == RegistrationStatus.REGISTERED and e.status == EventStatus.PUBLISHED
            out.append({"event": P.card(e, request), "registration": P.registration(r),
                        "online_url": e.online_url or None if private and e.mode != Mode.IN_PERSON else None})
        return Response({"results": out})


# ------------------------------------------------------------------ organisers (campaign and party)
def _candidate(request, candidate_id) -> Candidate:
    c = Candidate.objects.filter(pk=_uuid(candidate_id)).first()
    if c is None or not services.can_organise(request.user, c):
        raise ServiceError("Not found.", code="not_found", status_code=404)
    return c


def _organiser_event(request, pk, candidate=None, party=False) -> Event:
    e = Event.objects.select_related("candidate").filter(pk=_uuid(pk)).first()
    if e is None or (party and e.organiser_kind != Organiser.PARTY) or (candidate is not None and e.candidate_id != candidate.pk):
        raise ServiceError("Not found.", code="not_found", status_code=404)
    if party:
        if not services.can_organise(request.user, None):
            raise ServiceError("Not found.", code="not_found", status_code=404)
    elif not services.can_organise(request.user, e.candidate):
        raise ServiceError("Not found.", code="not_found", status_code=404)
    return e


ORGANISER_LIST = inline_serializer("OrganiserEventList", {"results": P.OrganiserEventSchema(many=True)})


class _OrganiserBase(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [WriteThrottle]
    party = False

    def scope(self, request, kwargs):
        return None if self.party else _candidate(request, kwargs["candidate_id"])

    def event(self, request, kwargs):
        cand = None if self.party else _candidate(request, kwargs["candidate_id"])
        return _organiser_event(request, kwargs["pk"], cand, party=self.party)


class OrganiserEvents(_OrganiserBase):
    @extend_schema(summary="Your campaign's events (drafts included)", responses=ORGANISER_LIST)
    def get(self, request, **kw):
        cand = self.scope(request, kw)
        qs = Event.objects.select_related("candidate").filter(organiser_kind=Organiser.PARTY) if self.party \
            else Event.objects.select_related("candidate").filter(candidate=cand)
        return Response({"results": [P.organiser_view(e, request) for e in qs.order_by("-starts_at")[:200]]})

    @extend_schema(summary="Create a draft event", request=P.EventWriteSchema, responses={201: P.OrganiserEventSchema})
    def post(self, request, **kw):
        cand = self.scope(request, kw)
        e = services.create(request.user, cand, _data(request))
        return Response(P.organiser_view(e, request), status=status.HTTP_201_CREATED)


def _data(request):
    d = {k: request.data.get(k) for k in services.FIELDS if k in request.data}
    for k in ("starts_at", "ends_at", "registration_closes_at"):
        if k in d:
            d[k] = _dt(d[k], k)
    if "capacity" in d and d["capacity"] in ("", None):
        d["capacity"] = None
    if "geo_lga" in d:  # spec 19 §7.2: the LGA picker's ID (geography IDs are integers)
        v = str(d["geo_lga"] or "").strip()
        if v and not v.isdigit():
            raise ServiceError("Choose an LGA from the list.", code="geo_mismatch", status_code=400)
        d["geo_lga"] = int(v) if v else None
    for k in ("members_only",):
        if k in d:
            d[k] = str(d[k]).lower() in ("true", "1", "on")
    for k in ("title", "description", "venue_name", "venue_address", "lga_text", "online_url", "accessibility_notes", "state",
              "venue_type", "address_visibility", "type", "mode", "registration"):
        if k in d and d[k] is None:
            d[k] = ""
    return d


def _dt(v, field):
    from django.utils.dateparse import parse_datetime
    from rest_framework import serializers

    if v in (None, ""):
        return None
    dt = parse_datetime(str(v))
    if dt is None:
        raise serializers.ValidationError({field: ["Use a date and time."]})
    return dt if dt.tzinfo else timezone.make_aware(dt)


class OrganiserEventDetail(_OrganiserBase):
    @extend_schema(summary="One of your events with counts", responses=P.OrganiserEventSchema)
    def get(self, request, **kw):
        return Response(P.organiser_view(self.event(request, kw), request))

    @extend_schema(summary="Edit (until it starts; date, time, venue or link changes notify registrants)", request=P.EventPatchSchema,
                   responses=P.OrganiserEventSchema)
    def patch(self, request, **kw):
        e = services.edit(request.user, self.event(request, kw), _data(request))
        return Response(P.organiser_view(e, request))

    @extend_schema(summary="Delete a draft (published events are cancelled instead)", responses={204: None})
    def delete(self, request, **kw):
        services.delete_draft(request.user, self.event(request, kw))
        return Response(status=status.HTTP_204_NO_CONTENT)


class OrganiserEventAction(_OrganiserBase):
    action_name = ""

    def get_throttles(self):
        extra = [UpdateThrottle()] if self.action_name == "updates" else []
        return super().get_throttles() + extra

    @extend_schema(summary="publish (with declarations), cancel (with reason), close-registration, updates (with text)",
                   request=inline_serializer("EventActionRequest", {"declarations": DS.DictField(required=False),
                                                                    "reason": DS.CharField(required=False), "text": DS.CharField(required=False)}),
                   responses=P.OrganiserEventSchema)
    def post(self, request, **kw):
        e = self.event(request, kw)
        a, d = self.action_name, request.data
        if a == "publish":
            e = services.publish(request.user, e, d.get("declarations") or {})
        elif a == "cancel":
            e = services.cancel(request.user, e, d.get("reason", ""))
        elif a == "close-registration":
            e = services.close_registration(request.user, e)
        elif a == "updates":
            services.add_update(request.user, e, d.get("text", ""))
        e = Event.objects.select_related("candidate").get(pk=e.pk)
        return Response(P.organiser_view(e, request))


CHECKIN = inline_serializer("EventCheckin", {"counts": DS.DictField(), "results": DS.ListField(child=DS.DictField())})


class OrganiserCheckin(_OrganiserBase):
    @extend_schema(summary="The check-in list: short names and ticket codes, 24 h before to 24 h after (audited)", responses=CHECKIN)
    def get(self, request, **kw):
        e = self.event(request, kw)
        rows = services.checkin_list(request.user, e, request=request)
        e.refresh_from_db()
        return Response({"counts": services.counts_by_status(e), "results": rows})

    @extend_schema(summary="Check someone in by ticket code", request=inline_serializer("CheckinRequest", {"code": DS.CharField()}),
                   responses=inline_serializer("CheckinResult", {"ticket_code": DS.CharField(), "name": DS.CharField(),
                                                                 "checked_in": DS.BooleanField()}))
    def post(self, request, **kw):
        return Response(services.check_in(request.user, self.event(request, kw), request.data.get("code", "")))


class OrganiserEventImage(_OrganiserBase):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(summary="Set the cover image (rights checkbox required)", request=OpenApiTypes.OBJECT, responses=P.OrganiserEventSchema)
    def put(self, request, **kw):
        from apps.media.http import upload_or_asset

        e = self.event(request, kw)
        try:
            upload, confirmed = upload_or_asset(request, field="image", purpose="event_image", target_id=e.pk)
            e = services.set_image(request.user, e, upload, rights_confirmed=confirmed)
        except PhotoError as exc:
            return Response({"error": {"status": 400, "code": "invalid_photo", "message": str(exc), "fields": {"image": [str(exc)]}}},
                            status=400)
        except OSError as exc:
            raise ServiceError("Image storage is unavailable right now. Try again later.", code="storage_unavailable",
                               status_code=503) from exc
        return Response(P.organiser_view(e, request))

    @extend_schema(summary="Remove the cover image", responses=P.OrganiserEventSchema)
    def delete(self, request, **kw):
        return Response(P.organiser_view(services.remove_image(request.user, self.event(request, kw)), request))


# party events: the same views for system admins under /admin/events/
class _Party:
    party = True
    permission_classes = [IsSystemAdmin]


class AdminPartyEvents(_Party, OrganiserEvents):
    pass


class AdminPartyEventDetail(_Party, OrganiserEventDetail):
    pass


class AdminPartyEventAction(_Party, OrganiserEventAction):
    pass


class AdminPartyCheckin(_Party, OrganiserCheckin):
    pass


class AdminPartyEventImage(_Party, OrganiserEventImage):
    pass


ADMIN_LIST = inline_serializer("AdminEventList", {"results": P.EventCardSchema(many=True), "counts": DS.DictField()})


class AdminAllEvents(_Private):
    """System admins: every event with counts only (never registrants)."""
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="All events (counts only), newest first; ?status=&state=", responses=ADMIN_LIST)
    def get(self, request):
        qs = Event.objects.select_related("candidate").exclude(status=EventStatus.DRAFT).order_by("-starts_at")
        if request.query_params.get("status") in EventStatus.values:
            qs = qs.filter(status=request.query_params["status"])
        if request.query_params.get("state"):
            qs = qs.filter(state=request.query_params["state"])
        return Response({"results": [{**P.card(e, request), "registered": e.registered_count, "waitlisted": e.waitlist_count,
                                      "blackout_flagged": e.flagged_blackout_at is not None} for e in qs[:200]],
                         "counts": services.stats()})
