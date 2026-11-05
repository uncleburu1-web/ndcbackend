"""Privacy and security API (spec 14 §7). Second-factor checks run at the authentication boundary by url name (access.security)."""
import uuid

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.access.models import Role
from apps.access.permissions import IsSignedIn
from apps.access.services import has_role
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from . import account, consent, incidents as I, register, requests as Q_
from .export import build_export, filename
from .models import DataRequest, Incident, Purpose


class _UserThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        u = getattr(request, "user", None)
        ident = f"u{u.pk}" if u is not None and u.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class ExportThrottle(_UserThrottle):
    scope = "data_export"

    def throttle_failure(self):
        raise ServiceError("You can download your data 3 times a day. Try again tomorrow.", "export_limit", 429)


class RequestThrottle(_UserThrottle):
    scope = "privacy_public"


class _Private(APIView):
    permission_classes = [IsSignedIn]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class IsPrivacyOfficer(IsSignedIn):
    """Privacy officers (implied by system admin while no separate DPO is named, §15.2)."""

    message = "Privacy officer access is required."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and has_role(request.user, Role.PRIVACY_OFFICER)


class _CsvByQuery(DefaultContentNegotiation):
    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class Page20(PageNumberPagination):
    page_size = 20


def _uuid(v):
    try:
        return uuid.UUID(str(v))
    except (TypeError, ValueError):
        raise ServiceError("Not found.", "not_found", 404) from None


def _purpose(p):
    if p not in Purpose.values:
        raise ServiceError("Not found.", "not_found", 404)
    return p


# ------------------------------------------------------------------ public
class PrivacyNotice(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        resp = Response(register.public_payload())
        patch_cache_control(resp, public=True, max_age=300)
        return resp


# ------------------------------------------------------------------ signed in
def security_notices(user):
    from apps.notifications.models import Kind, Notification

    rows = (Notification.objects.filter(recipient=user, kind=Kind.SECURITY_NOTICE, incident__isnull=False).select_related("incident")
            .order_by("-created_at")[:20])
    return [{"id": str(n.pk), "reference": n.incident.reference, "text": n.incident.notice_text, "sent_at": n.created_at} for n in rows]


class MyPrivacy(_Private):
    def get(self, request):
        u = request.user
        reqs = DataRequest.objects.filter(user=u).order_by("-received_at")
        return Response({
            "consents": consent.summary(u),
            "requests": [Q_.payload(r) for r in reqs.filter(self_served=False)[:50]],
            "downloads": [r.received_at for r in reqs.filter(self_served=True)[:50]],
            "consent_history": [{"purpose": c.purpose, "label": c.get_purpose_display(), "version": c.version, "granted_at": c.granted_at,
                                 "withdrawn_at": c.withdrawn_at, "effect": c.withdrawal_effect, "method": c.method}
                                for c in u.consents.all().order_by("-granted_at")[:50]],
            "security_notices": security_notices(u),
            "request_kinds": [{"value": k, "label": l} for k, l in DataRequest.Kind.choices],
            "delete": {"goes": account.WHAT_GOES, "stays": account.WHAT_STAYS},
            "privacy_version": register.PRIVACY_VERSION,
        })


class MyConsent(_Private):
    def get(self, request, purpose):
        _purpose(purpose)
        return Response({"purpose": purpose, "given": consent.has_consent(request.user, purpose),
                         "withdraw_effect": consent.preview_withdrawal(request.user, purpose)})

    def post(self, request, purpose):
        _purpose(purpose)
        rec = consent.grant(request.user, purpose)
        return Response({"purpose": purpose, "version": rec.version, "granted_at": rec.granted_at}, status=201)

    def delete(self, request, purpose):
        _purpose(purpose)
        delete_content = str(request.query_params.get("delete_content", "")).lower() in ("1", "true")  # communities (spec 17 §5.4)
        return Response(consent.withdraw(request.user, purpose, request=request, delete_content=delete_content))


class MyConsentPrompted(_Private):
    def post(self, request, purpose):
        _purpose(purpose)
        consent.mark_prompt_shown(request.user, purpose)
        return Response(status=204)


class MyDataExport(_Private):
    throttle_classes = [ExportThrottle]

    def post(self, request):
        u = request.user
        try:
            data = build_export(u)
        except ServiceError:
            raise
        except Exception:
            import logging

            logging.getLogger(__name__).error("Data export failed", exc_info=True)
            raise ServiceError("Your download couldn't be prepared just now. Try again later.", "export_unavailable", 503) from None
        Q_.open_request(u, DataRequest.Kind.ACCESS, self_served=True)  # the person's own history: a date, never the content
        resp = HttpResponse(data, content_type="application/zip")
        resp["Content-Disposition"] = f'attachment; filename="{filename(u)}"'
        return resp


class MyDelete(_Private):
    def post(self, request):
        return Response(account.delete_own_account(request.user, request, (request.data or {}).get("confirm", "")))


class MyPrivacyRequests(_Private):
    throttle_classes = [RequestThrottle]

    def get_throttles(self):
        return super().get_throttles() if self.request.method == "POST" else []

    def get(self, request):
        return Response({"results": [Q_.payload(r) for r in DataRequest.objects.filter(user=request.user, self_served=False)
                                     .order_by("-received_at")[:50]]})

    def post(self, request):
        d = request.data or {}
        kind = d.get("kind")
        if kind == DataRequest.Kind.WITHDRAW_CONSENT:
            raise ServiceError("Withdraw consents directly on this page.", "validation_error")
        r = Q_.open_request(request.user, kind, d.get("details", ""))
        return Response({"id": str(r.pk), "reference": r.reference, "due_at": r.due_at}, status=201)


class MyPrivacyRequestAnswer(_Private):
    def post(self, request, pk):
        r = Q_.answer(_uuid(pk), request.user, (request.data or {}).get("text", ""))
        return Response(Q_.payload(r))


# ------------------------------------------------------------------ admin: requests
TABS = {"open": [DataRequest.Status.OPEN], "waiting": [DataRequest.Status.WAITING], "closed": [DataRequest.Status.DONE, DataRequest.Status.REFUSED]}


class AdminRequests(_Private):
    permission_classes = [IsPrivacyOfficer]

    def get(self, request):
        tab = request.query_params.get("tab", "open")
        states = TABS.get(tab, TABS["open"])
        qs = DataRequest.objects.filter(status__in=states, self_served=False).select_related("user")
        qs = qs.order_by("due_at") if tab != "closed" else qs.order_by("-closed_at")
        p = Page20()
        page = p.paginate_queryset(qs, request, view=self)
        now = timezone.now()
        counts = {t: DataRequest.objects.filter(status__in=s, self_served=False).count() for t, s in TABS.items()}
        rows = [Q_.payload(r, now=now) | {"person_name": (r.user.full_name or "Deleted account") if r.user else "Deleted account"}
                for r in page]
        return Response({"count": p.page.paginator.count, "next": p.get_next_link(), "previous": p.get_previous_link(), "counts": counts,
                         "results": rows})


class AdminRequestDetail(_Private):
    permission_classes = [IsPrivacyOfficer]

    def get(self, request, pk):
        r = get_object_or_404(DataRequest.objects.select_related("user", "assigned_to"), pk=_uuid(pk), self_served=False)
        Q_.note_viewed(r, request.user, request)
        return Response(Q_.payload(r, officer=True) | {"linked_actions": [{"key": k, "label": l} for k, l in account.LINKED.items()]})


class AdminRequestAction(_Private):
    permission_classes = [IsPrivacyOfficer]
    action_name = ""

    def post(self, request, pk):
        d = request.data or {}
        a, by, pk = self.action_name, request.user, _uuid(pk)
        if a == "reply":
            r = Q_.reply(pk, by, d.get("text", ""))
        elif a == "ask-info":
            r = Q_.ask_info(pk, by, d.get("text", ""))
        elif a == "resume":
            r = Q_.resume(pk, by, d.get("text", ""))
        elif a == "close":
            r = Q_.close(pk, by, d.get("outcome", ""), d.get("text", ""))
        elif a == "actions":
            r = get_object_or_404(DataRequest, pk=pk)
            return Response(account.linked_action(r, by, d.get("action", ""), request=request))
        else:
            raise ServiceError("Not found.", "not_found", 404)
        r.refresh_from_db()
        return Response(Q_.payload(r, officer=True))


class AdminRequestExport(_Private):
    permission_classes = [IsPrivacyOfficer]

    def post(self, request, pk):
        r = get_object_or_404(DataRequest.objects.select_related("user"), pk=_uuid(pk))
        if r.user is None or r.user.clerk_deleted_at:
            raise ServiceError("This person's account has been deleted.", "invalid_transition", 409)
        data = build_export(r.user)
        from .models import DataRequestEvent

        DataRequestEvent.objects.create(request=r, event="officer_export", actor=request.user, note="Data export prepared by the officer")
        record(AuditLog.Action.PRIVACY_DATA_EXPORTED_BY_OFFICER, target=r, actor=request.user, request=request, reference=r.reference)
        resp = HttpResponse(data, content_type="application/zip")
        resp["Content-Disposition"] = f'attachment; filename="ndcconnect-data-{r.reference}.zip"'
        return resp


# ------------------------------------------------------------------ admin: incidents
class AdminIncidents(_Private):
    permission_classes = [IsPrivacyOfficer]
    content_negotiation_class = _CsvByQuery

    def get(self, request):
        if request.query_params.get("format") == "csv":
            resp = HttpResponse(I.export_csv(request.user, request), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="ndcconnect-breach-register-{timezone.localdate():%Y%m%d}.csv"'
            return resp
        tab = request.query_params.get("tab", "open")
        qs = Incident.objects.all()
        qs = qs.filter(status=Incident.Status.CLOSED) if tab == "closed" else qs.exclude(status=Incident.Status.CLOSED)
        now = timezone.now()
        return Response({"results": [I.payload(i, now=now) for i in qs[:200]],
                         "counts": {"open": Incident.objects.exclude(status=Incident.Status.CLOSED).count(),
                                    "closed": Incident.objects.filter(status=Incident.Status.CLOSED).count()},
                         "categories": [{"key": c.key, "label": c.label} for c in register.CATEGORIES],
                         "processors": [{"key": k or "none", "label": l} for k, l in
                                        [("", "None"), ("clerk", "Clerk"), ("paystack", "Paystack"), ("hosting", "Hosting provider")]]})

    def post(self, request):
        d = dict(request.data or {})
        if d.get("processor") == "none":
            d["processor"] = ""
        inc = I.log(request.user, d)
        return Response(I.payload(inc, detail=True), status=201)


class AdminIncidentDetail(_Private):
    permission_classes = [IsPrivacyOfficer]

    def get(self, request, pk):
        return Response(I.payload(get_object_or_404(Incident, pk=_uuid(pk)), detail=True))

    def patch(self, request, pk):
        d = dict(request.data or {})
        if d.get("processor") == "none":
            d["processor"] = ""
        return Response(I.payload(I.update(_uuid(pk), request.user, d), detail=True))


class AdminIncidentAction(_Private):
    permission_classes = [IsPrivacyOfficer]
    action_name = ""

    def post(self, request, pk):
        d = request.data or {}
        a, by, pk = self.action_name, request.user, _uuid(pk)
        if a == "steps":
            I.add_step(pk, by, d.get("text", ""), d.get("at"))
        elif a == "notifications":
            I.record_notification(pk, by, d.get("kind", ""), at=d.get("at"), how=d.get("how", ""), reference=d.get("reference", ""))
        elif a == "notice-preview":
            get_object_or_404(Incident, pk=pk)
            return Response(I.preview_notice(d.get("audience", ""), d.get("lines", "")))
        elif a == "notice":
            result = I.send_notice(pk, by, d.get("text", ""), d.get("audience", ""), d.get("lines", ""))
            return Response(result | {"incident": I.payload(Incident.objects.get(pk=pk), detail=True)})
        elif a == "close":
            I.close(pk, by, d.get("root_cause", ""), d.get("lessons", ""))
        else:
            raise ServiceError("Not found.", "not_found", 404)
        return Response(I.payload(Incident.objects.get(pk=pk), detail=True))


class AdminRegister(_Private):
    permission_classes = [IsPrivacyOfficer]

    def get(self, request):
        return Response(register.admin_payload())
