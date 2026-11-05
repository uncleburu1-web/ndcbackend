"""Reporting and moderation API (spec 13 §7). Public endpoints use the error envelope; admin endpoints re-check the
role table and rule 3 in the services, inside the transaction. Reporter identity never leaves the reveal endpoint."""
import mimetypes
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db.models import Count, Q
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.cache import add_never_cache_headers, patch_cache_control
from django.utils.dateparse import parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as DS
from rest_framework import status
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.access.services import is_system_admin
from apps.audit.models import AuditLog
from apps.audit.services import client_ip, record
from apps.candidates.models import Candidate
from apps.core import filters as F
from apps.core.exceptions import ServiceError
from apps.news.serializers import ReportSerializer
from apps.news.views import PublicThrottle, ReportThrottle

from . import rules as R
from . import services as S
from . import targets as T
from .models import (
    Appeal,
    Case,
    CaseStatus,
    LegalNotice,
    LegalNoticeItem,
    Report,
    Restriction,
    TargetKind,
)


class _Private(APIView):
    permission_classes = [IsSignedIn]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class Page20(PageNumberPagination):
    page_size = 20


class ModerationThrottle(SimpleRateThrottle):
    scope = "moderation_public"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        ident = f"u{user.pk}" if user is not None and user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class IsModerationStaff(IsSignedIn):
    message = "Moderation access is required."

    def has_permission(self, request, view):
        u = request.user
        return super().has_permission(request, view) and S.is_moderation_staff(u)


def _uuid(value, field="id"):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ServiceError("Not found.", code="not_found", status_code=404) from None


# ------------------------------------------------------------------ public
class RulesView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request):
        data = R.public_payload() | {"legal_email": settings.MODERATION_LEGAL_EMAIL,
                                     "counter_notice_days": settings.COPYRIGHT_COUNTER_NOTICE_DAYS, "appeal_days": S.APPEAL_DAYS}
        response = Response(data)
        patch_cache_control(response, public=True, max_age=300)
        return response


class CandidateReport(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [ReportThrottle]

    def post(self, request, slug):
        c = Candidate.objects.filter(slug=slug).first()
        if c is None or not T.is_public(TargetKind.CANDIDATE, c):
            raise ServiceError("Candidate not found.", code="not_found", status_code=404)
        ser = ReportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user if request.user and request.user.is_authenticated else None
        out = S.report(TargetKind.CANDIDATE, c, ser.validated_data["reason"], ser.validated_data.get("details", ""), user=user,
                       contact_email="" if user else ser.validated_data.get("contact_email", ""), ip=client_ip(request) or "")
        return Response(out, status=status.HTTP_201_CREATED)


class ReportStatusView(_Private):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicThrottle]

    def get(self, request):
        st = S.report_status(request.query_params.get("reference", ""))
        if st is None:
            raise ServiceError("We couldn't find a report with that reference.", code="not_found", status_code=404)
        return Response({"reference": st["reference"], "status_label": st["status_label"]})


class MyReports(_Private):
    def get(self, request):
        return Response({"results": S.my_reports(request.user)})


class CopyrightNotice(_Private):
    permission_classes = [AllowAny]
    throttle_classes = [ModerationThrottle]

    def post(self, request):
        d = request.data
        form = {k: (d.get(k) if isinstance(d.get(k), bool) else str(d.get(k, "") or "")[:1000])
                for k in ("name", "contact", "work", "url", "signature", "authority", "good_faith", "accurate")}
        user = request.user if request.user and request.user.is_authenticated else None
        n = S.submit_copyright_notice(form, ip=client_ip(request) or "", user=user, request=request)
        return Response({"reference": n.reference}, status=status.HTTP_201_CREATED)


class MyModeration(_Private):
    def get(self, request):
        return Response(S.author_view(request.user))


class MyAppeals(_Private):
    throttle_classes = [ModerationThrottle]

    def post(self, request):
        d = request.data
        case = restriction = None
        if d.get("case"):
            case = get_object_or_404(Case, pk=_uuid(d["case"]))
        elif d.get("restriction"):
            restriction = get_object_or_404(Restriction, pk=_uuid(d["restriction"]))
        a = S.open_appeal(request.user, str(d.get("statement", "")), str(d.get("evidence_url", "") or ""), case=case, restriction=restriction)
        return Response({"id": a.id, "status": a.status}, status=status.HTTP_201_CREATED)


class MyCounterNotices(_Private):
    throttle_classes = [ModerationThrottle]

    def post(self, request):
        n = get_object_or_404(LegalNotice, pk=_uuid(request.data.get("notice")))
        S.counter_notice(n, request.user, str(request.data.get("text", "")))
        return Response({"ok": True}, status=status.HTTP_201_CREATED)


# ------------------------------------------------------------------ admin: cases
def _due_words(case, now):
    due = min(d for d in (case.due_at, case.legal_due_at) if d) if case.status != CaseStatus.CLOSED else None
    if due is None:
        return None
    hours = int((due - now).total_seconds() // 3600)
    return {"due_at": due, "overdue": due < now, "hours": abs(hours)}


def _top_reason(case):
    top = case.reports.values("reason").annotate(n=Count("pk")).order_by("-n", "reason").first()
    return {"reason": top["reason"], "label": R.reason_label(top["reason"], case.target_kind)} if top else None


def case_row(case, now, user):
    return {
        "id": case.id, "target_kind": case.target_kind, "kind_label": case.get_target_kind_display(), "title": case.title, "state": case.state,
        "status": case.status, "priority": case.priority, "priority_label": R.PRIORITY_LABEL[case.priority],
        "legal_due_at": case.legal_due_at, "due": _due_words(case, now), "opened_at": case.opened_at, "closed_at": case.closed_at,
        "decision": case.decision, "rule": case.rule, "source": case.source,
        "report_count": getattr(case, "n_reports", None) if getattr(case, "n_reports", None) is not None else case.reports.count(),
        "top_reason": _top_reason(case), "burst_flag": case.burst_flag, "legal_hold": case.legal_hold, "restorable": case.restorable,
        "assigned_to": {"id": case.assigned_to_id, "email": case.assigned_to.email, "me": case.assigned_to_id == user.pk}
        if case.assigned_to_id else None,
    }


class AdminCases(_Private):
    permission_classes = [IsModerationStaff]

    def get(self, request):
        qp, user, now = request.query_params, request.user, timezone.now()
        qs = S.visible_cases(user).select_related("assigned_to").annotate(n_reports=Count("reports"))
        tab = qp.get("tab", "open")
        if tab == "mine":
            qs = qs.filter(assigned_to=user).exclude(status=CaseStatus.CLOSED)
        elif tab == "escalated":
            if not is_system_admin(user):
                raise ServiceError("Escalated cases are for system administrators.", code="permission_denied", status_code=403)
            qs = qs.filter(status=CaseStatus.ESCALATED)
        elif tab == "closed":
            qs = qs.filter(status=CaseStatus.CLOSED).order_by("-closed_at")
        else:
            qs = qs.exclude(status=CaseStatus.CLOSED)
            if not is_system_admin(user):
                qs = qs.filter(status=CaseStatus.OPEN)
        if tab != "closed":
            qs = qs.order_by("priority_rank", "opened_at")
        kind = {"news": "article", "posts": "post", "profiles": "candidate"}.get(qp.get("type", ""), qp.get("type", ""))
        if kind == "communities":  # spec 17: community posts and comments together
            qs = qs.filter(target_kind__in=T.COMMUNITY_KINDS)
        elif kind in TargetKind.values:
            qs = qs.filter(target_kind=kind)
        if qp.get("community"):  # moderation history for one community and the levels below (SRS 2.2.5)
            from apps.communities.models import (
                Community,
                CommunityComment,
                CommunityPost,
            )
            from apps.communities.services import under_paths

            com = Community.objects.filter(pk=_uuid(qp["community"], "community")).first()
            if com is not None:
                qs = qs.filter(Q(target_kind=TargetKind.COMMUNITY_POST,
                                 target_id__in=CommunityPost.objects.filter(under_paths([com.path], "community__path")).values("pk"))
                               | Q(target_kind=TargetKind.COMMUNITY_COMMENT,
                                   target_id__in=CommunityComment.objects.filter(under_paths([com.path], "post__community__path")).values("pk")))
        if qp.get("priority") == "urgent":
            qs = qs.filter(priority__in=["urgent", "legal"])
        elif qp.get("priority") in R.PRIORITY_RANK:
            qs = qs.filter(priority=qp["priority"])
        if qp.get("reason"):
            qs = qs.filter(reports__reason=qp["reason"]).distinct()
        if qp.get("overdue") in ("1", "true"):
            qs = qs.filter(Q(due_at__lt=now) | Q(legal_due_at__lt=now)).exclude(status=CaseStatus.CLOSED)
        if qp.get("state"):
            qs = qs.filter(state=qp["state"])
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response([case_row(c, now, user) for c in page])
        base = S.visible_cases(user)
        response.data["counts"] = {
            "open": base.exclude(status=CaseStatus.CLOSED).filter(**({} if is_system_admin(user) else {"status": CaseStatus.OPEN})).count(),
            "mine": base.filter(assigned_to=user).exclude(status=CaseStatus.CLOSED).count(),
            "escalated": base.filter(status=CaseStatus.ESCALATED).count() if is_system_admin(user) else 0,
            "appeals": len(_appeals_for(user, pending_only=True)),
        }
        response.data["is_system_admin"] = is_system_admin(user)
        return response


def _item_payload(case, obj, request):
    """The item as readers see it (removed items as they were before removal)."""
    if obj is None:
        return None
    k = case.target_kind
    out = {"kind": k, "kind_words": T.kind_words(k, obj), "title": T.title(k, obj), "public_url": T.public_url(k, obj),
           "admin_url": T.admin_url(k, obj), "public": T.is_public(k, obj), "status": obj.status}
    if k == TargetKind.ARTICLE:
        out |= {"body": obj.body, "source": obj.source, "state": obj.state, "source_url": obj.source_url,
                "author": ({"email": obj.author.email, "name": obj.author.full_name} if obj.author and obj.source == "community" else None),
                "official": obj.source == "official"}
    elif k == TargetKind.POST:
        out |= {"body": obj.body, "candidate": {"slug": obj.candidate.slug, "full_name": obj.candidate.full_name},
                "author": {"label": f"Campaign account for {obj.candidate.full_name}"}}
    elif k in T.COMMUNITY_KINDS:  # spec 17 §17.2: the author's short name, never contact details
        from apps.communities.text import short_name

        com = T.community_of(k, obj)
        out |= {"body": obj.body, "community": {"id": com.id, "name": com.name, "path": com.path, "level": com.level},
                "author": {"label": short_name(obj.author) + (f" · Campaign team · {obj.campaign_candidate.full_name}"
                                                               if obj.campaign_candidate_id else "")},
                "hidden_at": obj.hidden_at}
    else:
        out |= {"body": obj.biography, "position": obj.get_position_display(), "author": None}
    return out


def _report_payload(r):
    # Reporter masked (spec 13 §9): moderators see only signed-in vs guest, plus a contact email the guest chose to give.
    return {"id": r.id, "reference": r.reference, "reason": r.reason, "reason_label": R.reason_label(r.reason, r.target_kind),
            "details": r.details, "created_at": r.created_at, "status": r.status,
            "reporter_label": "Signed-in member" if r.reporter_id else ("Guest (contact given)" if r.contact_email else "Guest"),
            "contact_email": r.contact_email, "signed_in": bool(r.reporter_id)}


def _case_detail(case, request):
    user, now = request.user, timezone.now()
    obj = T.load(case.target_kind, case.target_id)
    out = case_row(case, now, user)
    out |= {"item": _item_payload(case, obj, request), "reports": [_report_payload(r) for r in case.reports.all()],
            "note_to_author": case.note_to_author, "internal_note": case.internal_note,
            "decided_by": case.decided_by.email if case.decided_by else None, "decided_at": case.decided_at,
            "escalated": case.escalated_by_id is not None,
            "history": [{"event": e.event, "note": e.note, "created_at": e.created_at, "actor": e.actor.email if e.actor else None}
                        for e in case.events.select_related("actor")],
            "notices": [{"id": i.notice_id, "reference": i.notice.reference, "kind": i.notice.kind, "kind_label": i.notice.get_kind_display(),
                         "outcome": i.outcome} for i in case.notice_items.select_related("notice")],
            "appeals": [{"id": a.id, "status": a.status, "status_label": a.get_status_display(), "statement": a.statement,
                         "evidence_url": a.evidence_url, "created_at": a.created_at, "decision_reason": a.decision_reason}
                        for a in case.appeals.all()]}
    standing = None
    community_author = case.target_kind in T.COMMUNITY_KINDS and obj is not None and obj.author_id
    if (case.target_kind == TargetKind.ARTICLE and obj is not None and obj.source == "community" and obj.author_id) or community_author:
        pause_kind = Restriction.Kind.COMMUNITY_PAUSE if community_author else Restriction.Kind.NEWS_PAUSE
        standing = S.standing(obj.author)
        standing["user_id"] = obj.author_id
        standing["pause_kind"] = pause_kind
        standing["active_pause"] = next(({"id": r.id, "ends_at": r.ends_at} for r in Restriction.objects.filter(
            user_id=obj.author_id, kind=pause_kind, lifted_at__isnull=True, ends_at__gt=now)), None)
    out["standing"] = standing
    # what this viewer may do (the server re-checks every action)
    blocked = ""
    try:
        S._check(user, case, obj)
    except ServiceError as exc:
        blocked = exc.get_codes() if isinstance(exc.get_codes(), str) else "permission_denied"
        out["blocked_message"] = str(exc.detail)
    out["blocked"] = blocked
    out["is_system_admin"] = is_system_admin(user)
    out["rules"] = [{"id": r.id, "title": r.title} for r in R.RULES]
    return out


def _case_for(request, pk):
    case = get_object_or_404(Case.objects.select_related("assigned_to", "decided_by"), pk=pk)
    if not S.can_view(request.user, case):
        raise ServiceError("Case not found.", code="not_found", status_code=404)
    return case


class AdminCaseDetail(_Private):
    permission_classes = [IsModerationStaff]

    def get(self, request, pk):
        return Response(_case_detail(_case_for(request, pk), request))


class AdminCaseAction(_Private):
    permission_classes = [IsModerationStaff]
    action_name = ""

    def post(self, request, pk):
        case, d, u = _case_for(request, pk), request.data, request.user
        note = str(d.get("note", "") or "")[:1000]
        a = self.action_name
        if a == "claim":
            S.claim(case, u, request)
        elif a == "release":
            S.release(case, u)
        elif a == "keep":
            S.keep(case, u, note, request=request)
        elif a == "remove":
            S.remove(case, u, str(d.get("rule", "")), str(d.get("note_to_author", "") or "")[:500], str(d.get("internal_note", "") or "")[:1000],
                     warn=bool(d.get("warn")), request=request)
        elif a == "restore":
            S.restore(case, u, note, request=request)
        elif a == "escalate":
            S.escalate(case, u, note, request=request)
        elif a == "warn":
            obj = T.load(case.target_kind, case.target_id)
            S._check(u, case, obj)
            authors = T.authors(case.target_kind, obj)
            if not authors:
                raise ServiceError("There's no author account to warn.", code="invalid_transition", status_code=409)
            for author in authors:
                S.warn_user(author, u, str(d.get("rule", "")), str(d.get("reason", "") or note), case=case, request=request)
        elif a == "pause":
            try:
                days = int(d.get("days", 0))
            except (TypeError, ValueError):
                days = 0
            if case.target_kind in T.COMMUNITY_KINDS:
                S.pause_community(case, u, days, str(d.get("rule", "")), str(d.get("reason", "") or note),
                                  scope="all" if d.get("scope") == "all" else "community", request=request)
            else:
                S.pause_news(case, u, days, str(d.get("rule", "")), str(d.get("reason", "") or note), request=request)
        elif a == "hide":
            S.hide(case, u, note, request=request)
        elif a == "unhide":
            S.unhide(case, u, note, request=request)
        elif a == "lift-pause":
            r = get_object_or_404(Restriction, pk=_uuid(d.get("restriction")), case=case)
            S._check(u, case, T.load(case.target_kind, case.target_id))
            S.lift_pause(r, u, note, request=request)
        elif a == "hold":
            if not is_system_admin(u):
                raise ServiceError("Only system administrators can set legal holds.", code="permission_denied", status_code=403)
            S.set_hold(case, u, bool(d.get("on")), request=request)
        return Response(_case_detail(Case.objects.select_related("assigned_to", "decided_by").get(pk=case.pk), request))


class AdminCaseHideAction(AdminCaseAction):
    """Spec 17 §8.3: Hide / Unhide a community item while deciding. Same answer as the case detail."""

    @extend_schema(summary="Hide or unhide a community post or comment while deciding (spec 17 §8.3)",
                   request=inline_serializer("CaseHideRequest", {"note": DS.CharField(required=False)}),
                   responses=OpenApiResponse(OpenApiTypes.OBJECT, description="The case, as GET /admin/moderation/cases/{id}/ returns it"))
    def post(self, request, pk):
        return super().post(request, pk)


class AdminRevealReporter(_Private):
    permission_classes = [IsSystemAdmin]

    def post(self, request, pk, report_id):
        r = get_object_or_404(Report.objects.select_related("reporter", "case"), pk=report_id, case_id=pk)
        return Response(S.reveal_reporter(r, request.user, request=request))


# ------------------------------------------------------------------ admin: appeals
def _appeals_for(user, pending_only=False):
    qs = Appeal.objects.select_related("case", "restriction__case", "appellant").order_by("created_at")
    if pending_only:
        qs = qs.filter(status=Appeal.Status.PENDING)
    out = []
    for a in qs[:500]:
        case = a.case or (a.restriction.case if a.restriction_id else None)
        if case is not None and not S.can_view(user, case):
            continue
        if case is None and not is_system_admin(user):
            continue
        out.append(a)
    return out


def _appeal_row(a, user):
    case = a.case or (a.restriction.case if a.restriction_id else None)
    original = a.case if a.case_id else a.restriction
    return {"id": a.id, "kind": a.kind, "kind_label": a.get_kind_display(), "status": a.status, "status_label": a.get_status_display(),
            "statement": a.statement, "evidence_url": a.evidence_url, "created_at": a.created_at, "decided_at": a.decided_at,
            "decision_reason": a.decision_reason, "case": case.id if case else None, "title": case.title if case else "",
            "target_kind": case.target_kind if case else None,
            "original": {"rule": original.rule, "rule_title": R.rule_title(original.rule),
                         "note": getattr(original, "note_to_author", "") or getattr(original, "reason", ""),
                         "decided_at": getattr(original, "decided_at", None) or getattr(original, "created_at", None),
                         "ends_at": getattr(original, "ends_at", None)},
            "can_decide": a.status == Appeal.Status.PENDING and S.can_decide_appeal(user, a),
            "own_decision": user.pk == S._original_decider(a)}


class AdminAppeals(_Private):
    permission_classes = [IsModerationStaff]

    def get(self, request):
        pending = F.choice(request, "status", ["pending", "decided"], default="pending") == "pending"
        rows = [_appeal_row(a, request.user) for a in _appeals_for(request.user, pending_only=pending)]
        if not pending:
            rows = [r for r in rows if r["status"] != "pending"][::-1]
        return Response({"results": rows})


class AdminAppealDecide(_Private):
    permission_classes = [IsModerationStaff]

    def post(self, request, pk):
        a = get_object_or_404(Appeal, pk=pk)
        upheld = request.data.get("upheld")
        if not isinstance(upheld, bool):
            raise ServiceError("Choose Upheld or Not upheld.", code="validation_error", status_code=400)
        a = S.decide_appeal(a, request.user, upheld, str(request.data.get("reason", "") or ""), request=request)
        return Response(_appeal_row(Appeal.objects.select_related("case", "restriction__case").get(pk=a.pk), request.user))


# ------------------------------------------------------------------ admin: legal notices
def _notice_row(n, now):
    return {"id": n.id, "reference": n.reference, "kind": n.kind, "kind_label": n.get_kind_display(), "sender_name": n.sender_name,
            "sender_org": n.sender_org, "received_at": n.received_at, "due_at": n.due_at, "status": n.status,
            "overdue": bool(n.due_at and n.status == "open" and n.due_at < now), "acknowledged_at": n.acknowledged_at,
            "items": n.items.count(), "pending_items": n.items.filter(outcome=LegalNoticeItem.Outcome.PENDING).count()}


def _notice_detail(n, now):
    out = _notice_row(n, now) | {
        "sender_contact": n.sender_contact, "sender_reference": n.sender_reference, "legal_basis": n.legal_basis,
        "acknowledged_how": n.acknowledged_how, "has_attachment": bool(n.attachment), "work_description": n.work_description,
        "declaration_accepted": n.declaration_accepted, "signature": n.signature, "counter_notice_at": n.counter_notice_at,
        "counter_notice_text": n.counter_notice_text, "owner_notified_at": n.owner_notified_at,
        "counter_notice_days": settings.COPYRIGHT_COUNTER_NOTICE_DAYS,
        "items_detail": [{"id": i.id, "outcome": i.outcome, "outcome_label": i.get_outcome_display(), "outcome_reason": i.outcome_reason,
                          "case": {"id": i.case.id, "title": i.case.title, "target_kind": i.case.target_kind, "status": i.case.status,
                                   "decision": i.case.decision, "legal_hold": i.case.legal_hold, "restorable": i.case.restorable}}
                         for i in n.items.select_related("case")]}
    return out


class AdminLegalNotices(_Private):
    permission_classes = [IsSystemAdmin]

    def get(self, request):
        now = timezone.now()
        qs = LegalNotice.objects.all()
        status = F.choice(request, "status", ["open", "closed"])
        if status:
            qs = qs.filter(status=status)
        paginator = Page20()
        page = paginator.paginate_queryset(qs.order_by("status", "due_at", "-received_at"), request, view=self)
        return paginator.get_paginated_response([_notice_row(n, now) for n in page])

    def post(self, request):
        d = request.data
        received = parse_datetime(str(d.get("received_at", ""))) if d.get("received_at") else timezone.now()
        if received is None:
            raise ServiceError("Enter when the notice was received.", code="validation_error", status_code=400)
        if timezone.is_naive(received):
            received = timezone.make_aware(received)
        items = d.get("items") or []
        if isinstance(items, str):
            items = [x for x in items.split() if x.strip()]
        n = S.log_notice(kind=str(d.get("kind", "")), sender_name=str(d.get("sender_name", "")).strip(),
                         sender_org=str(d.get("sender_org", "") or ""), sender_contact=str(d.get("sender_contact", "")).strip(),
                         sender_reference=str(d.get("sender_reference", "") or ""), legal_basis=str(d.get("legal_basis", "") or "").strip(),
                         received_at=received, items=[str(x) for x in items][:50], by=request.user,
                         work_description=str(d.get("work_description", "") or ""), declaration_accepted=bool(d.get("declaration_accepted")),
                         signature=str(d.get("signature", "") or ""), request=request)
        return Response(_notice_detail(n, timezone.now()), status=status.HTTP_201_CREATED)


class AdminLegalNoticeDetail(_Private):
    permission_classes = [IsSystemAdmin]

    def get(self, request, pk):
        return Response(_notice_detail(get_object_or_404(LegalNotice, pk=pk), timezone.now()))


class AdminLegalNoticeAction(_Private):
    permission_classes = [IsSystemAdmin]
    action_name = ""

    def post(self, request, pk):
        n = get_object_or_404(LegalNotice, pk=pk)
        if self.action_name == "acknowledge":
            how = str(request.data.get("how", "") or "").strip()
            if not how:
                raise ServiceError("Say how it was acknowledged.", code="validation_error", status_code=400)
            S.acknowledge(n, request.user, how, request=request)
        elif self.action_name == "hold":
            on = bool(request.data.get("on"))
            for item in n.items.select_related("case"):
                S.set_hold(item.case, request.user, on, request=request)
        elif self.action_name == "owner-notified":
            S.mark_owner_notified(n, request.user, request=request)
        return Response(_notice_detail(LegalNotice.objects.get(pk=n.pk), timezone.now()))


class AdminLegalNoticeItem(_Private):
    permission_classes = [IsSystemAdmin]

    def patch(self, request, pk, item_id):
        item = get_object_or_404(LegalNoticeItem, pk=item_id, notice_id=pk)
        S.set_item_outcome(item, request.user, str(request.data.get("outcome", "")), str(request.data.get("reason", "") or ""), request=request)
        return Response(_notice_detail(LegalNotice.objects.get(pk=pk), timezone.now()))


ATTACHMENT_TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}
MAGIC = {b"%PDF": "application/pdf", b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png"}


def _storage():
    return FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT, base_url=None)


class AdminLegalAttachment(_Private):
    """Private storage, type-checked by content, ≤ 10 MB; every view audited (spec 13 §9)."""

    permission_classes = [IsSystemAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, pk):
        n = get_object_or_404(LegalNotice, pk=pk)
        f = request.FILES.get("file")
        if f is None:
            raise ServiceError("Choose a file.", code="validation_error", status_code=400)
        if f.size > 10 * 1024 * 1024:
            raise ServiceError("The file must be 10 MB or smaller.", code="validation_error", status_code=400)
        head = f.read(8)
        f.seek(0)
        kind = next((t for sig, t in MAGIC.items() if head.startswith(sig)), None)
        if kind is None:
            raise ServiceError("Upload a PDF, JPEG or PNG.", code="validation_error", status_code=400)
        st = _storage()
        if n.attachment:
            st.delete(n.attachment)
        n.attachment = st.save(f"legal/{n.pk}{ATTACHMENT_TYPES[kind]}", f)
        n.save(update_fields=["attachment"])
        record(AuditLog.Action.MODERATION_LEGAL_NOTICE_UPDATED, target=n, actor=request.user, request=request, change="attachment")
        return Response(_notice_detail(n, timezone.now()))

    def get(self, request, pk):
        n = get_object_or_404(LegalNotice, pk=pk)
        if not n.attachment:
            raise ServiceError("No attachment.", code="not_found", status_code=404)
        record(AuditLog.Action.MODERATION_ATTACHMENT_VIEWED, target=n, actor=request.user, request=request)
        ctype = mimetypes.guess_type(n.attachment)[0] or "application/octet-stream"
        resp = FileResponse(_storage().open(n.attachment, "rb"), content_type=ctype)
        resp["Content-Disposition"] = f'attachment; filename="{n.reference}{n.attachment[-4:]}"'
        resp["X-Content-Type-Options"] = "nosniff"
        return resp


# ------------------------------------------------------------------ admin: compliance
class _CsvByQuery(DefaultContentNegotiation):
    """`?format=csv` is ours (spec 13 §7), not DRF's renderer override."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class AdminCompliance(_Private):
    permission_classes = [IsSystemAdmin]
    content_negotiation_class = _CsvByQuery

    def get(self, request):
        now_year = timezone.localtime().year
        try:
            year = int(request.query_params.get("year", now_year))
        except ValueError:
            year = now_year
        if not 2020 <= year <= now_year:
            raise ServiceError("Choose a year from 2020 to this year.", code="validation_error", status_code=400)
        if request.query_params.get("format") == "csv":
            record(AuditLog.Action.MODERATION_COMPLIANCE_EXPORTED, actor=request.user, request=request, year=year)
            resp = HttpResponse(S.compliance_csv(year), content_type="text/csv; charset=utf-8")
            resp["Content-Disposition"] = f'attachment; filename="ndcconnect-nitda-compliance-{year}.csv"'
            return resp
        return Response(S.compliance_report(year) | {"years": list(range(now_year, 2025, -1))})
