"""Volunteering API — spec 18 §9, §13. Everything here is per-user (`private, no-store`)."""
import uuid

from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as DS
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn
from apps.candidates.models import Candidate
from apps.core.exceptions import ServiceError

from . import services
from .models import Availability, Role, VolunteerApplication, VolunteerUpdate


class ApplyThrottle(UserRateThrottle):
    scope = "volunteer_apply"


class UpdateThrottle(UserRateThrottle):
    scope = "campaign_update"


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


def _uuid(v):
    try:
        return uuid.UUID(str(v))
    except ValueError:
        raise ServiceError("Not found.", code="not_found", status_code=404) from None


def _short(user):
    from apps.communities.text import short_name

    return short_name(user)


ROLES = [{"value": r.value, "label": r.label} for r in Role]
AVAILABILITY = [{"value": a.value, "label": a.label} for a in Availability]


def opening_payload(candidate) -> dict:
    o = services.opening(candidate)
    return {"open": services.is_open(candidate), "intro": o.intro, "roles": [{"value": r, "label": Role(r).label} for r in o.roles],
            "states": o.states, "all_roles": ROLES, "availability": AVAILABILITY,
            "candidate": {"id": str(candidate.pk), "slug": candidate.slug, "full_name": candidate.full_name}}


def application_mine(a) -> dict:
    return {"id": str(a.pk), "candidate": {"id": str(a.candidate_id), "slug": a.candidate.slug, "full_name": a.candidate.full_name},
            "roles": a.roles, "state": a.state, "lga": a.lga_text, "availability": a.availability, "note": a.note,
            "share_phone": a.share_phone, "status": a.status, "status_label": a.get_status_display(), "decline_reason": a.decline_reason,
            "created_at": a.created_at, "decided_at": a.decided_at}


def application_for_campaign(a) -> dict:
    """§9.3: the minimum. Full name and phone only through the audited contact endpoint."""
    return {"id": str(a.pk), "name": _short(a.user), "state": a.state, "state_label": a.get_state_display(), "lga": a.lga_text,
            "roles": a.roles, "availability": a.availability, "note": a.note, "status": a.status, "status_label": a.get_status_display(),
            "can_share_phone": a.share_phone and a.status == "accepted", "created_at": a.created_at, "decided_at": a.decided_at}


OPENING = inline_serializer("VolunteerOpening", {"open": DS.BooleanField(), "intro": DS.CharField(), "roles": DS.ListField(child=DS.DictField()),
                                                 "states": DS.ListField(child=DS.CharField()), "all_roles": DS.ListField(child=DS.DictField()),
                                                 "availability": DS.ListField(child=DS.DictField()), "candidate": DS.DictField()})
MINE = inline_serializer("VolunteerApplicationMine", {"id": DS.UUIDField(), "candidate": DS.DictField(), "roles": DS.ListField(child=DS.CharField()),
                                                      "state": DS.CharField(), "lga": DS.CharField(), "availability": DS.ListField(child=DS.CharField()),
                                                      "note": DS.CharField(), "share_phone": DS.BooleanField(), "status": DS.CharField(),
                                                      "status_label": DS.CharField(), "decline_reason": DS.CharField(),
                                                      "created_at": DS.DateTimeField(), "decided_at": DS.DateTimeField(allow_null=True)})
CAMPAIGN_ROW = inline_serializer("VolunteerApplicationRow", {"id": DS.UUIDField(), "name": DS.CharField(), "state": DS.CharField(),
                                                             "state_label": DS.CharField(), "lga": DS.CharField(),
                                                             "roles": DS.ListField(child=DS.CharField()),
                                                             "availability": DS.ListField(child=DS.CharField()), "note": DS.CharField(),
                                                             "status": DS.CharField(), "status_label": DS.CharField(),
                                                             "can_share_phone": DS.BooleanField(), "created_at": DS.DateTimeField(),
                                                             "decided_at": DS.DateTimeField(allow_null=True)})
APPLY = inline_serializer("VolunteerApply", {"roles": DS.ListField(child=DS.ChoiceField(Role.choices)), "state": DS.CharField(),
                                             "lga_text": DS.CharField(required=False, allow_blank=True),
                                             "geo_lga": DS.IntegerField(required=False, allow_null=True),
                                             "availability": DS.ListField(child=DS.ChoiceField(Availability.choices), required=False),
                                             "note": DS.CharField(required=False, allow_blank=True), "share_phone": DS.BooleanField(required=False)})


def _published(slug) -> Candidate:
    from apps.events.services import candidate_published

    c = Candidate.objects.filter(slug=slug).first()
    if c is None or not candidate_published(c):
        raise ServiceError("Not found.", code="not_found", status_code=404)
    return c


class CandidateVolunteering(_Private):
    permission_classes = [AllowAny]

    @extend_schema(summary="A campaign's volunteer opening (intro, roles, states, open)", responses=OPENING)
    def get(self, request, slug):
        return Response(opening_payload(_published(slug)))


class CandidateVolunteerApply(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [ApplyThrottle]

    @extend_schema(summary="Apply to volunteer; consent `volunteering` first", request=APPLY, responses={201: MINE})
    def post(self, request, slug):
        a = services.apply(request.user, _published(slug), request.data)
        return Response(application_mine(VolunteerApplication.objects.select_related("candidate").get(pk=a.pk)),
                        status=status.HTTP_201_CREATED)


MY = inline_serializer("MyVolunteering", {"results": DS.ListField(child=MINE), "updates": DS.ListField(child=DS.DictField())})


class MyVolunteering(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Your applications, decisions and the updates campaigns sent you", responses=MY)
    def get(self, request):
        apps = VolunteerApplication.objects.filter(user=request.user).select_related("candidate").order_by("-created_at")
        accepted = [a.candidate_id for a in apps if a.status == "accepted"]
        updates = []
        for u in VolunteerUpdate.objects.filter(candidate_id__in=accepted).select_related("candidate").order_by("-created_at")[:50]:
            mine = next(a for a in apps if a.candidate_id == u.candidate_id)
            if u.created_at >= (mine.decided_at or mine.created_at) and (not u.role or u.role in mine.roles):
                updates.append({"candidate": u.candidate.full_name, "text": u.text, "created_at": u.created_at})
        return Response({"results": [application_mine(a) for a in apps], "updates": updates})


class MyVolunteerWithdraw(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Withdraw an application (contact details disappear at once)", request=None, responses=MINE)
    def post(self, request, pk):
        a = VolunteerApplication.objects.select_related("candidate").filter(pk=_uuid(pk), user=request.user).first()
        if a is None:
            raise ServiceError("Not found.", code="not_found", status_code=404)
        a = services.withdraw(request.user, a)
        return Response(application_mine(VolunteerApplication.objects.select_related("candidate").get(pk=a.pk)))


# ------------------------------------------------------------------ the campaign
def _campaign(request, candidate_id) -> Candidate:
    from apps.events.services import can_organise

    c = Candidate.objects.filter(pk=_uuid(candidate_id)).first()
    if c is None or not can_organise(request.user, c):
        raise ServiceError("Not found.", code="not_found", status_code=404)
    return c


class CampaignVolunteering(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Your campaign's volunteer opening", responses=OPENING)
    def get(self, request, candidate_id):
        c = _campaign(request, candidate_id)
        out = opening_payload(c)
        out["open"] = services.opening(c).open
        return Response(out)

    @extend_schema(summary="Change the opening (open, intro, roles, states)",
                   request=inline_serializer("VolunteerOpeningWrite", {"open": DS.BooleanField(required=False), "intro": DS.CharField(required=False),
                                                                       "roles": DS.ListField(child=DS.CharField(), required=False),
                                                                       "states": DS.ListField(child=DS.CharField(), required=False)}),
                   responses=OPENING)
    def patch(self, request, candidate_id):
        c = _campaign(request, candidate_id)
        o = services.set_opening(request.user, c, request.data)
        out = opening_payload(c)
        out["open"] = o.open
        return Response(out)


ROWS = inline_serializer("VolunteerApplicationRows", {"results": DS.ListField(child=CAMPAIGN_ROW), "counts": DS.DictField()})


class CampaignVolunteers(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Applications to your campaign (short names only)", responses=ROWS,
                   parameters=[OpenApiParameter("status", str), OpenApiParameter("role", str), OpenApiParameter("state", str)])
    def get(self, request, candidate_id):
        c = _campaign(request, candidate_id)
        qs = VolunteerApplication.objects.filter(candidate=c).select_related("user").order_by("created_at")
        qp = request.query_params
        counts = {s: qs.filter(status=s).count() for s in ("pending", "accepted", "declined", "withdrawn", "ended")}
        if qp.get("status"):
            qs = qs.filter(status=qp["status"])
        if qp.get("role"):
            qs = qs.filter(roles__contains=[qp["role"]])
        if qp.get("state"):
            qs = qs.filter(state=qp["state"])
        return Response({"results": [application_for_campaign(a) for a in qs[:500]], "counts": counts})


def _app(c, pk) -> VolunteerApplication:
    a = VolunteerApplication.objects.select_related("user", "candidate").filter(pk=_uuid(pk), candidate=c).first()
    if a is None:
        raise ServiceError("Not found.", code="not_found", status_code=404)
    return a


class CampaignVolunteerDecide(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="Accept or decline (reason optional, shown to the person)",
                   request=inline_serializer("VolunteerDecide", {"accept": DS.BooleanField(), "reason": DS.CharField(required=False)}),
                   responses=CAMPAIGN_ROW)
    def post(self, request, candidate_id, pk):
        c = _campaign(request, candidate_id)
        a = services.decide(request.user, _app(c, pk), bool(request.data.get("accept")), request.data.get("reason", ""))
        return Response(application_for_campaign(VolunteerApplication.objects.select_related("user").get(pk=a.pk)))


class CampaignVolunteerContact(_Private):
    permission_classes = [IsSignedIn]

    @extend_schema(summary="An accepted volunteer's full name, and phone if they chose to share it (audited)",
                   responses=inline_serializer("VolunteerContact", {"full_name": DS.CharField(), "phone_number": DS.CharField(allow_null=True)}))
    def get(self, request, candidate_id, pk):
        c = _campaign(request, candidate_id)
        return Response(services.contact(request.user, _app(c, pk), request=request))


class CampaignVolunteerUpdates(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [UpdateThrottle]

    @extend_schema(summary="Updates sent to your accepted volunteers", responses=inline_serializer(
        "VolunteerUpdates", {"results": DS.ListField(child=DS.DictField()), "left_this_week": DS.IntegerField()}))
    def get(self, request, candidate_id):
        from datetime import timedelta

        from django.utils import timezone

        c = _campaign(request, candidate_id)
        rows = VolunteerUpdate.objects.filter(candidate=c)[:50]
        used = VolunteerUpdate.objects.filter(candidate=c, created_at__gte=timezone.now() - timedelta(days=7)).count()
        return Response({"results": [{"text": u.text, "role": u.role, "sent_to": u.sent_to, "created_at": u.created_at} for u in rows],
                         "left_this_week": max(services.UPDATES_PER_WEEK - used, 0)})

    @extend_schema(summary="Send an update to accepted volunteers (all, or one role)",
                   request=inline_serializer("VolunteerUpdateWrite", {"text": DS.CharField(), "role": DS.CharField(required=False)}),
                   responses={201: inline_serializer("VolunteerUpdateSent", {"text": DS.CharField(), "role": DS.CharField(),
                                                                             "created_at": DS.DateTimeField()})})
    def post(self, request, candidate_id):
        c = _campaign(request, candidate_id)
        u = services.send_update(request.user, c, request.data.get("text", ""), request.data.get("role", "") or "")
        return Response({"text": u.text, "role": u.role, "created_at": u.created_at}, status=status.HTTP_201_CREATED)
