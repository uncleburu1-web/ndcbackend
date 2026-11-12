"""Notifications API — spec 07 §7.5. Acts only on request.user; private, no-store."""
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as S
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn
from apps.core.exceptions import ServiceError

from . import services
from .models import Kind

# OpenAPI shapes (spec 15 §14.1; the web app's generated types use these)
_Photo = inline_serializer("NotificationCandidatePhoto", {"url_200": S.CharField()}, allow_null=True)
NOTIFICATION = inline_serializer("Notification", {
    "id": S.UUIDField(), "kind": S.ChoiceField(Kind.choices), "created_at": S.DateTimeField(), "read_at": S.DateTimeField(allow_null=True),
    "decision": S.CharField(help_text="A short word: approved, rejected, verified, removed, activated, cancelled, received …"),
    "article": inline_serializer("NotificationArticle", {"id": S.UUIDField(), "source": S.CharField(), "title": S.CharField(),
                                                          "public": S.BooleanField()}, allow_null=True),
    "post": inline_serializer("NotificationPost", {"id": S.UUIDField(), "kind": S.CharField(), "kind_label": S.CharField(),
                                                    "title": S.CharField()}, required=False),
    "candidate": inline_serializer("NotificationCandidate", {"slug": S.CharField(), "full_name": S.CharField(), "photo": _Photo},
                                   required=False),
    "case": inline_serializer("NotificationCase", {"id": S.UUIDField(), "target_kind": S.CharField(), "title": S.CharField(),
                                                    "rule": S.CharField(), "rule_title": S.CharField()}, required=False),
    "appeal": inline_serializer("NotificationAppeal", {"id": S.UUIDField(), "status": S.CharField(), "reason": S.CharField()},
                                required=False),
    "privacy_request": inline_serializer("NotificationPrivacyRequest", {"id": S.CharField(), "reference": S.CharField(),
                                                                         "status": S.CharField(), "status_label": S.CharField()},
                                         required=False),
    "security_notice": inline_serializer("NotificationSecurityNotice", {"reference": S.CharField(), "text": S.CharField()},
                                         required=False),
    "membership": inline_serializer("NotificationMembership", {"status": S.CharField()}, required=False),
    "donation": inline_serializer("NotificationDonation", {"reference": S.CharField()}, required=False, allow_null=True),
    "community_post": inline_serializer("NotificationCommunityPost", {"id": S.UUIDField(), "count": S.IntegerField()}, required=False),
    "community": inline_serializer("NotificationCommunity", {"id": S.UUIDField(), "name": S.CharField(), "path": S.CharField()},
                                   required=False),
    "event": inline_serializer("NotificationEvent", {"id": S.UUIDField(), "title": S.CharField(), "starts_at": S.DateTimeField(),
                                                      "status": S.CharField(), "organiser": S.CharField()}, required=False),
    "volunteer_update": inline_serializer("NotificationVolunteerUpdate", {"text": S.CharField()}, required=False),
    "geo": inline_serializer("NotificationGeo", {"record": S.ChoiceField(choices=["membership", "pvc"]),
                                                  "change": S.ChoiceField(choices=["", "retired", "moved"])}, required=False),
})
NOTIFICATION_PAGE = inline_serializer("NotificationPage", {"count": S.IntegerField(), "next": S.CharField(allow_null=True),
                                                           "previous": S.CharField(allow_null=True), "results": S.ListField(child=NOTIFICATION)})
PREFERENCES = inline_serializer("NotificationPreferences", {"announcements": S.BooleanField(), "community_replies": S.BooleanField(),
                                                           "event_reminders": S.BooleanField(), "candidate_events": S.BooleanField(),
                                                           "community_posts": S.BooleanField(), "volunteer_updates": S.BooleanField(),
                                                           "candidate_bells_paused": S.BooleanField()})


class _Private(APIView):
    permission_classes = [IsSignedIn]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class Page20(PageNumberPagination):
    page_size = 20


def item(n, request=None):
    a = n.article
    out = {"id": n.id, "kind": n.kind, "created_at": n.created_at, "read_at": n.read_at, "decision": n.decision,
           "article": {"id": a.id, "source": a.source, "title": a.title, "public": a.status == "successful"} if a else None}
    p = n.candidate_post
    if p is not None:  # spec 09 §7.4
        from apps.candidates.photos import public_url

        c = p.candidate
        out["post"] = {"id": p.id, "kind": p.kind, "kind_label": p.get_kind_display(), "title": p.title}
        out["candidate"] = {"slug": c.slug, "full_name": c.full_name,
                            "photo": {"url_200": public_url(request, c.photo_200)} if c.photo_200 else None}
    case = n.case or (n.appeal.case if n.appeal_id else None)
    if case is not None:  # spec 13 §4.6: what, which rule, and where to go next; never who decided
        from apps.moderation.rules import rule_title

        removed_for_reporter = n.kind == "report_outcome" and n.decision == "removed"
        out["case"] = {"id": case.id, "target_kind": case.target_kind, "title": "" if removed_for_reporter else case.title,
                       "rule": case.rule, "rule_title": rule_title(case.rule) if case.rule else ""}
    if n.appeal_id:
        out["appeal"] = {"id": n.appeal.id, "status": n.appeal.status, "reason": n.appeal.decision_reason}
    if n.data_request_id:  # spec 14 §4.4: reference and status only; the update itself is on Your data
        r = n.data_request
        out["privacy_request"] = {"id": str(r.pk), "reference": r.reference, "status": r.status, "status_label": r.get_status_display()}
    if n.incident_id:  # the officer's notice text, never the internal incident record
        out["security_notice"] = {"reference": n.incident.reference, "text": n.incident.notice_text}
    if n.kind == "membership_decision":  # spec 15 §9.2: the word only; details are on the membership page
        out["membership"] = {"status": n.membership.status if n.membership_id else ""}
    if n.kind == "donation_receipt":  # the person's own receipt link; no amount in the row
        out["donation"] = {"reference": n.donation.reference} if n.donation_id else None
    if n.community_post_id:  # spec 17 §10: where to go; never the post's text
        out["community_post"] = {"id": n.community_post_id, "count": n.count}
        c = n.community_post.community
        out["community"] = {"id": c.id, "name": c.name, "path": c.path}
    elif n.kind == "community_placed":
        out["community"] = None
    if n.event_id:  # spec 18 §10: which event and a short word; never who else registered
        e = n.event
        out["event"] = {"id": e.id, "title": e.title, "starts_at": e.starts_at, "status": e.status, "organiser": e.organiser_label}
    if n.candidate_id and n.kind in ("volunteer_decision", "volunteer_update"):
        c = n.candidate
        out["candidate"] = {"slug": c.slug, "full_name": c.full_name, "photo": None}
    if n.volunteer_update_id:
        out["volunteer_update"] = {"text": n.volunteer_update.text}
    if n.kind in ("geo_confirm", "geo_unit_changed"):  # spec 19 §10: which record, and what happened; never the unit
        rec, _, ch = (n.decision or "").partition(".")
        out["geo"] = {"record": "pvc" if rec == "pvc" else "membership", "change": {"ret": "retired", "mov": "moved"}.get(ch, "")}
    return out


class NotificationList(_Private):
    @extend_schema(summary="Your notifications, newest first (20 a page)", responses=NOTIFICATION_PAGE)
    def get(self, request):
        paginator = Page20()
        page = paginator.paginate_queryset(services.visible(request.user).order_by("-created_at"), request, view=self)
        return paginator.get_paginated_response([item(n, request) for n in page])


class UnreadCount(_Private):
    @extend_schema(summary="How many unread notifications you have (capped at 100)",
                   responses=inline_serializer("UnreadCount", {"unread": S.IntegerField()}))
    def get(self, request):
        return Response({"unread": services.unread_count(request.user)})


class MarkRead(_Private):
    @extend_schema(summary="Mark one notification read", request=None, responses=inline_serializer("Ok", {"ok": S.BooleanField()}))
    def post(self, request, pk):
        from .models import Notification

        if not Notification.objects.filter(recipient=request.user, pk=pk).exists():
            raise ServiceError("Notification not found.", code="not_found", status_code=404)
        services.mark_read(request.user, pk)
        return Response({"ok": True})


class MarkAllRead(_Private):
    @extend_schema(summary="Mark all your notifications read", request=None,
                   responses=inline_serializer("MarkedRead", {"updated": S.IntegerField()}))
    def post(self, request):
        return Response({"updated": services.mark_all_read(request.user)})


class Preferences(_Private):
    @extend_schema(summary="Your notification settings", responses=PREFERENCES)
    def get(self, request):
        return Response(services.preferences(request.user))

    @extend_schema(summary="Turn notification categories on or off (alias of /me/settings/notifications/)", request=PREFERENCES, responses=PREFERENCES)
    def patch(self, request):
        values = {k: request.data.get(k) for k in services.PREF_FIELDS if k in request.data}
        if not values or any(not isinstance(v, bool) for v in values.values()):
            raise ServiceError("Send one of " + ", ".join(services.PREF_FIELDS) + " as true or false.", code="validation_error",
                               status_code=400)
        return Response(services.set_preferences(request.user, **values))
