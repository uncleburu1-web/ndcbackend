"""Membership API — spec 06 §7."""
import csv
import io
import zipfile
from datetime import date

from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.models import Role
from apps.access.permissions import HasRole, IsProfileComplete, IsSignedIn, IsSystemAdmin
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError
from apps.donations.views import csv_safe

from . import services
from .models import Membership, MembershipPayment, MembershipStatus as M
from .serializers import AdminMembershipSerializer, AdminPaymentRowSerializer, DetailsSerializer, MyMembershipSerializer

OfficerOnly = HasRole(Role.MEMBERSHIP_OFFICER)


class PayThrottle(UserRateThrottle):
    scope = "membership_pay"


class RevealThrottle(UserRateThrottle):
    scope = "pii_reveal"


class Page20(PageNumberPagination):
    page_size = 20


def _private(response):
    add_never_cache_headers(response)
    patch_cache_control(response, private=True, no_store=True)
    return response


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        return _private(super().finalize_response(request, response, *args, **kwargs))


def _me_payload(user):
    m = services.membership_for(user)
    data = MyMembershipSerializer(m).data if m else {"status": None}
    data["display_status"] = services.display_status(user)
    return data


# ----------------------------------------------------------------- public
class MembershipConfigView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        response = Response(services.config_payload())
        patch_cache_control(response, public=True, max_age=60)
        return response


class PaymentStatusView(_Private):
    """GET /membership/payments/{reference}/ — the reference is the key; no personal data (spec 06 §7.1)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, reference):
        p = get_object_or_404(MembershipPayment.objects.select_related("membership"), reference=reference)
        p = services.maybe_reverify(p)
        m = Membership.objects.get(pk=p.membership_id)
        return Response({"reference": p.reference, "status": p.status, "status_label": p.get_status_display(),
                         "amount_kobo": p.amount_kobo, "membership_status": m.status, "paid_at": p.paid_at})


# ------------------------------------------------------------------- user
class MyMembership(_Private):
    def get_permissions(self):
        return [IsSignedIn()] if self.request.method == "GET" else [IsProfileComplete()]

    def get(self, request):
        return Response(_me_payload(request.user))

    def put(self, request):
        ser = DetailsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        services.save_details(request.user, ser.validated_data, request=request)
        return Response(_me_payload(request.user))


class MyMembershipPhoto(_Private):
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        return [IsSignedIn()] if self.request.method == "GET" else [IsProfileComplete()]

    def put(self, request):
        upload = request.FILES.get("photo")
        if upload is None:
            raise ServiceError("Choose a photo to upload.", code="photo_invalid", status_code=400)
        consent = str(request.data.get("consent", "")).lower() in ("true", "1", "on", "yes")
        services.set_photo(request.user, upload, consent=consent)
        return Response(_me_payload(request.user))

    def get(self, request):
        m = services.membership_for(request.user)
        data = services.read_photo(m) if m else None
        if not data:
            return _private(HttpResponse(status=404))
        return _private(HttpResponse(data, content_type="image/webp"))


class MyMembershipPay(_Private):
    permission_classes = [IsProfileComplete]
    throttle_classes = [PayThrottle]

    def post(self, request):
        p = services.start_payment(request.user, request=request)
        return Response({"reference": p.reference, "checkout_url": p.checkout_url}, status=status.HTTP_201_CREATED)


class MyMembershipCancel(_Private):
    permission_classes = [IsSignedIn]

    def post(self, request):
        m = services.membership_for(request.user)
        if m is None:
            raise ServiceError("You don't have a membership to cancel.", code="invalid_transition", status_code=409)
        services.cancel(m, actor=request.user, reason=str(request.data.get("reason", "")), request=request)
        return Response(_me_payload(request.user))


class MyMembershipCard(_Private):
    permission_classes = [IsSignedIn]

    def get(self, request):
        m = services.membership_for(request.user)
        if m is None or m.status != M.ACTIVE:
            return Response({"error": {"status": 404, "code": "not_found", "message": "You don't have an active membership.",
                                       "fields": {}}}, status=404)
        return Response({"full_name": m.full_name, "membership_number": m.membership_number, "state": m.get_state_display(),
                         "lga": m.lga, "ward": m.ward, "member_since": m.activated_at, "has_photo": bool(m.photo), "status": m.status})


# ------------------------------------------------------------------ admin
def _admin_qs(request):
    qp = request.query_params
    qs = Membership.objects.select_related("user", "user__pvc").order_by("-created_at")
    if qp.get("status") in M.values:
        qs = qs.filter(status=qp["status"])
    if qp.get("state"):
        qs = qs.filter(state=qp["state"])
    for param, lookup in (("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
        try:
            if qp.get(param):
                qs = qs.filter(**{lookup: date.fromisoformat(qp[param])})
        except ValueError:
            raise ServiceError("Use YYYY-MM-DD dates.", code="validation_error", status_code=400) from None
    q = (qp.get("q") or "").strip()[:100]
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(full_name__icontains=q) | Q(user__email__icontains=q) | Q(membership_number__icontains=q))
    return qs


class AdminMembershipList(_Private):
    permission_classes = [OfficerOnly]

    def get(self, request):
        from django.db.models import Count

        qs = _admin_qs(request)
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminMembershipSerializer(page, many=True).data)
        response.data["totals"] = dict(Membership.objects.values_list("status").annotate(n=Count("id")))
        return response


class AdminMembershipDetail(_Private):
    permission_classes = [OfficerOnly]

    def get(self, request, pk):
        return Response(AdminMembershipSerializer(get_object_or_404(Membership.objects.select_related("user"), pk=pk), detail=True).data)


class AdminMembershipAction(_Private):
    permission_classes = [OfficerOnly]
    action_name = ""

    def get_throttles(self):
        return [RevealThrottle()] if self.action_name == "reveal-nin" else []

    def post(self, request, pk):
        m = get_object_or_404(Membership.objects.select_related("user"), pk=pk)
        reason = str(request.data.get("reason", ""))
        if self.action_name == "reveal-nin":
            return Response({"nin": services.reveal_nin(m, officer=request.user, request=request)})
        if self.action_name == "approve":
            m = services.approve(m, officer=request.user, request=request)
        elif self.action_name == "reject":
            m = services.reject(m, officer=request.user, reason=reason, request=request)
        elif self.action_name == "cancel":
            m = services.cancel(m, actor=request.user, reason=reason, request=request)
        elif self.action_name == "recheck-payment":
            m = services.recheck_payment(m, actor=request.user, request=request)
        return Response(AdminMembershipSerializer(Membership.objects.select_related("user").get(pk=m.pk), detail=True).data)


class AdminMembershipPhoto(_Private):
    permission_classes = [OfficerOnly]

    def get(self, request, pk):
        m = get_object_or_404(Membership, pk=pk)
        data = services.read_photo(m)
        if not data:
            return _private(HttpResponse(status=404))
        record(AuditLog.Action.MEMBERSHIP_PHOTO_VIEWED, target=m, actor=request.user, request=request)
        return _private(HttpResponse(data, content_type="image/webp"))


class AdminMembershipSettingsView(_Private):
    def get_permissions(self):
        return [OfficerOnly()] if self.request.method == "GET" else [IsSystemAdmin()]

    def get(self, request):
        return Response(services.config_payload())

    def patch(self, request):
        value = request.data.get("requires_approval")
        if not isinstance(value, bool):
            raise ServiceError("requires_approval must be true or false.", code="validation_error", status_code=400)
        services.set_requires_approval(value, admin=request.user, request=request)
        return Response(services.config_payload())


class AdminMembershipPayments(_Private):
    permission_classes = [OfficerOnly]

    def get(self, request):
        qs = MembershipPayment.objects.select_related("membership").order_by("-created_at")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(AdminPaymentRowSerializer(page, many=True).data)


class _Echo:
    def write(self, value):
        return value


class AdminRegisterCsv(_Private):
    """INEC register (Electoral Act 2026 s. 77(2)) — active members only; audited; contains NINs (spec 06 §4.4)."""

    permission_classes = [OfficerOnly]

    def get(self, request):
        state = request.query_params.get("state", "")
        qs = services.register_queryset(state)
        record(AuditLog.Action.MEMBERSHIP_REGISTER_EXPORTED, actor=request.user, request=request, kind="csv", state=state,
               rows=qs.count())
        writer = csv.writer(_Echo())

        def rows():
            yield "﻿" + writer.writerow(services.REGISTER_COLUMNS)
            for m in qs.iterator(chunk_size=500):
                yield writer.writerow([csv_safe(v) for v in services.register_row(m)])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="ndc-register-{state or "all"}-{date.today().isoformat()}.csv"'
        return _private(response)


class AdminRegisterPhotos(_Private):
    permission_classes = [OfficerOnly]

    def get(self, request):
        state = request.query_params.get("state", "")
        qs = services.register_queryset(state).exclude(photo="")
        record(AuditLog.Action.MEMBERSHIP_REGISTER_EXPORTED, actor=request.user, request=request, kind="photos", state=state,
               rows=qs.count())
        buf = io.BytesIO()  # built in memory per request; never written to the server's disk
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for m in qs.iterator(chunk_size=200):
                data = services.read_photo(m)
                if data:
                    zf.writestr(f"{m.membership_number}.webp", data)
        response = HttpResponse(buf.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="ndc-register-photos-{state or "all"}-{date.today().isoformat()}.zip"'
        return _private(response)
