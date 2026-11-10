"""Donation API — spec 05 §7."""
import csv
import logging
from datetime import date

from django.db.models import Count, Q, Sum
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsFinanceViewer, IsSignedIn
from apps.audit.models import AuditLog
from apps.audit.services import record

from . import services
from .models import Designation, Donation, DonationStatus as S
from .serializers import (
    AdminDonationEventSerializer,
    AdminDonationSerializer,
    DonationStatusSerializer,
    MyDonationSerializer,
    PublicDonationSerializer,
    StartDonationSerializer,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------- throttles
class _PerIP(AnonRateThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class StartIPThrottle(_PerIP):
    scope = "donations_start"


class StartUserThrottle(SimpleRateThrottle):
    scope = "donations_start"

    def get_cache_key(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return None  # guests are covered by the per-IP throttle
        return self.cache_format % {"scope": f"{self.scope}_user", "ident": request.user.pk}


class ReceiptThrottle(_PerIP):
    scope = "donations_receipt"


class FinanceThrottle(_PerIP):
    scope = "finance_public"


class Page20(PageNumberPagination):
    page_size = 20


def _public(response):
    patch_cache_control(response, public=True, max_age=60)
    return response


def _private(response):
    add_never_cache_headers(response)
    patch_cache_control(response, private=True, no_store=True)
    return response


# --------------------------------------------------------------- public/donor
class DonationConfigView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return _public(Response(services.config_payload()))


class StartDonationView(APIView):
    """POST /donations/ — guests welcome; a valid token links the donor account (§6.2)."""

    permission_classes = [AllowAny]
    throttle_classes = [StartIPThrottle, StartUserThrottle]

    def post(self, request):
        if not services.donations_open():  # before validation, so a closed gate says so plainly
            return _private(Response({"error": {"status": 503, "code": "donations_unavailable",
                                                "message": "Donations aren't open yet.", "fields": {}}}, status=503))
        ser = StartDonationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = services.start_donation(data=ser.validated_data, user=request.user, request=request)
        return _private(Response({"reference": d.reference, "checkout_url": d.checkout_url}, status=status.HTTP_201_CREATED))


class DonationStatusView(APIView):
    """GET /donations/{reference}/ — the receipt link is the key (§7.1)."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ReceiptThrottle]

    def get(self, request, reference):
        d = get_object_or_404(Donation.objects.select_related("candidate"), reference=reference)
        d = services.maybe_reverify(d)
        return _private(Response(DonationStatusSerializer(d).data))


class MyDonationsView(APIView):
    permission_classes = [IsSignedIn]

    def get(self, request):
        qs = Donation.objects.filter(donor_user=request.user).exclude(status=S.INITIATED).select_related("candidate")
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        return _private(paginator.get_paginated_response(MyDonationSerializer(page, many=True).data))


def _public_qs(request):
    qs = Donation.objects.filter(status=S.SUCCESSFUL).select_related("candidate").order_by("-paid_at", "-id")
    designation = request.query_params.get("designation")
    if designation in (Designation.PARTY, Designation.CANDIDATE):
        qs = qs.filter(designation=designation)
    slug = request.query_params.get("candidate")
    if slug:
        qs = qs.filter(candidate__slug=slug)
    return qs


def _net_total(qs):
    agg = qs.aggregate(a=Sum("amount_kobo"), r=Sum("refunded_kobo"), n=Count("id"))
    return {"total_kobo": (agg["a"] or 0) - (agg["r"] or 0), "count": agg["n"]}


def _by_candidate(qs):
    rows = (qs.filter(designation=Designation.CANDIDATE).values("candidate__slug", "candidate__full_name")
            .annotate(a=Sum("amount_kobo"), r=Sum("refunded_kobo"), n=Count("id")).order_by("candidate__full_name"))
    return [{"slug": r["candidate__slug"], "full_name": r["candidate__full_name"], "total_kobo": (r["a"] or 0) - (r["r"] or 0),
             "count": r["n"]} for r in rows]


class FinanceDonationsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [FinanceThrottle]

    def get(self, request):
        paginator = Page20()
        page = paginator.paginate_queryset(_public_qs(request), request, view=self)
        return _public(paginator.get_paginated_response(PublicDonationSerializer(page, many=True).data))


class FinanceSummaryView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [FinanceThrottle]

    def get(self, request):
        # Real totals never include demo data. `demo` is null unless simulated rows exist (local previews only).
        ok = Donation.objects.filter(status=S.SUCCESSFUL)
        real, demo = ok.filter(is_demo=False), ok.filter(is_demo=True)
        has_demo = demo.exists()
        return _public(Response({
            "overall": _net_total(real),
            "filtered": _net_total(_public_qs(request).filter(is_demo=False)),
            "party": _net_total(real.filter(designation=Designation.PARTY)),
            "candidates": _by_candidate(real),
            "demo": {
                "overall": _net_total(demo),
                "filtered": _net_total(_public_qs(request).filter(is_demo=True)),
                "candidates": _by_candidate(demo),
            } if has_demo else None,
        }))


# ------------------------------------------------------------- admin/finance
def _admin_qs(request):
    qp = request.query_params
    qs = Donation.objects.select_related("candidate", "donor_user").order_by("-created_at")
    if qp.get("status") in S.values:
        qs = qs.filter(status=qp["status"])
    if qp.get("designation") in Designation.values:
        qs = qs.filter(designation=qp["designation"])
    if qp.get("candidate"):
        qs = qs.filter(candidate__slug=qp["candidate"])
    for key, lookup in (("date_from", "created_at__date__gte"), ("date_to", "created_at__date__lte")):
        if qp.get(key):
            try:
                qs = qs.filter(**{lookup: date.fromisoformat(qp[key])})
            except ValueError:
                pass
    q = (qp.get("q") or "").strip()[:100]
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(donor_email__icontains=q) | Q(donor_name__icontains=q))
    return qs


class AdminDonationList(APIView):
    permission_classes = [IsFinanceViewer]

    def get(self, request):
        qs = _admin_qs(request)
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminDonationSerializer(page, many=True).data)
        real = qs.filter(is_demo=False)  # demo data is listed (badged) but never counted
        response.data["totals"] = {"successful": _net_total(real.filter(status=S.SUCCESSFUL)), "review": real.filter(status=S.REVIEW).count(),
                                   "demo": _net_total(qs.filter(is_demo=True, status=S.SUCCESSFUL))}
        return _private(response)


class AdminDonationDetail(APIView):
    permission_classes = [IsFinanceViewer]

    def get(self, request, pk):
        d = get_object_or_404(Donation.objects.select_related("candidate", "donor_user"), pk=pk)
        record(AuditLog.Action.DONATION_VIEWED, target=d, actor=request.user, request=request)
        data = AdminDonationSerializer(d).data
        data["events"] = AdminDonationEventSerializer(d.events.all(), many=True).data
        return _private(Response(data))


class AdminDonationRecheck(APIView):
    permission_classes = [IsFinanceViewer]

    def post(self, request, pk):
        d = services.recheck(get_object_or_404(Donation, pk=pk), actor=request.user, request=request)
        data = AdminDonationSerializer(d).data
        data["events"] = AdminDonationEventSerializer(d.events.all(), many=True).data
        return _private(Response(data))


CSV_COLUMNS = ["reference", "paid_at", "status", "amount_naira", "refunded_naira", "designation", "candidate", "election_year",
               "donor_name", "donor_email", "donor_phone", "donor_state", "donor_address", "public_label", "display_choice", "channel",
               "provider_reference"]


def csv_safe(value) -> str:
    """Stop spreadsheet formula injection (§7.4)."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _naira(kobo: int) -> str:
    return f"{kobo // 100}.{kobo % 100:02d}"


class _Echo:
    def write(self, value):
        return value


class AdminDonationExport(APIView):
    permission_classes = [IsFinanceViewer]

    def get(self, request):
        qs = _admin_qs(request).filter(is_demo=False)  # financial records only: demo data is never exported
        count = qs.count()
        filters = {k: v for k, v in request.query_params.items() if k in ("status", "designation", "candidate", "date_from", "date_to", "q")}
        record(AuditLog.Action.DONATION_EXPORTED, actor=request.user, request=request, filters=filters, rows=count)
        writer = csv.writer(_Echo())

        def rows():
            yield "﻿" + writer.writerow(CSV_COLUMNS)
            for d in qs.iterator(chunk_size=500):
                yield writer.writerow([csv_safe(v) for v in (
                    d.reference, d.paid_at.isoformat() if d.paid_at else "", d.status, _naira(d.amount_kobo), _naira(d.refunded_kobo),
                    d.designation, d.candidate.full_name if d.candidate_id else "", d.election_year or "", d.donor_name, d.donor_email,
                    d.donor_phone, d.donor_state, d.donor_address, d.public_label, d.display_choice, d.channel, d.provider_reference)])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="ndc-donations-{date.today().isoformat()}.csv"'
        return _private(response)
