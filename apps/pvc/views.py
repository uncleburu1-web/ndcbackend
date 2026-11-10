"""PVC verification API — spec 06 §7.1–7.2, stand-alone since spec 08 §7."""
from django.conf import settings
from django.db.models import Min
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.core import filters as F
from apps.access.models import Role
from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.access.services import has_role
from apps.candidates.reference import State
from apps.core.exceptions import ServiceError

from . import services
from .checkers import get_checker
from .models import PvcSettings, PvcStatus, PvcVerification
from .serializers import AdminPvcSerializer, MyPvcSerializer, PvcSubmitSerializer


class PvcSubmitThrottle(UserRateThrottle):
    scope = "pvc_submit"

    def allow_request(self, request, view):
        return True if request.method != "PUT" else super().allow_request(request, view)


class RevealThrottle(UserRateThrottle):
    scope = "pii_reveal"


class Page20(PageNumberPagination):
    page_size = 20


class _Private(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


class MyPvc(_Private):
    permission_classes = [IsSignedIn]
    throttle_classes = [PvcSubmitThrottle]

    def get(self, request):
        rec = PvcVerification.objects.filter(user=request.user).first()
        return Response(MyPvcSerializer(rec).data if rec else {"status": "unverified", "show_badge": False})

    def put(self, request):
        ser = PvcSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        from apps.geo.records import read_choice

        rec = services.submit(request.user, vin=v["vin"], surname=v["surname"], state=v["state"], lga=v.get("lga", ""),
                              polling_unit=v.get("polling_unit", ""), geo=read_choice(request.data), request=request)
        return Response(MyPvcSerializer(rec).data, status=status.HTTP_200_OK)

    def delete(self, request):
        services.withdraw(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MyPvcBadge(_Private):
    """PATCH /me/pvc/badge/ — public "Verified voter" opt-in (spec 08 §7)."""

    permission_classes = [IsSignedIn]

    def patch(self, request):
        value = request.data.get("show_badge")
        if not isinstance(value, bool):
            raise ServiceError("show_badge must be true or false.", code="validation_error", status_code=400)
        return Response(MyPvcSerializer(services.set_badge(request.user, value)).data)


def config_payload() -> dict:
    s = PvcSettings.current()
    return {"inec_pvc_url": settings.INEC_PVC_URL or None, "inec_voter_check_url": settings.INEC_VOTER_CHECK_URL,
            "guidance_note": s.guidance_note, "collection_note": s.collection_note}


class PvcConfig(APIView):
    """GET /pvc/config/ — public links and guidance for "Get your PVC" (spec 08 §4.1)."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        response = Response(config_payload())
        patch_cache_control(response, public=True, max_age=300)
        return response


class PvcOfficer(IsSignedIn):
    """PVC officers or membership officers (system admins imply both) — spec 08 §5.3."""

    message = "PVC checks are for PVC officers, membership officers and system administrators."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and (
            has_role(request.user, Role.PVC_OFFICER) or has_role(request.user, Role.MEMBERSHIP_OFFICER))


class AdminPvcList(_Private):
    permission_classes = [PvcOfficer]

    def get(self, request):
        qs = (PvcVerification.objects.select_related("user", "reviewed_by", "geo_lga", "geo_ward", "geo_polling_unit", "pending_geo_lga",
                                                     "pending_geo_ward", "pending_geo_polling_unit").order_by("created_at"))
        st = F.choice(request, "status", [*PvcStatus.values, "all"], default="pending")
        if st in PvcStatus.values:
            qs = qs.filter(status=st)
        state = F.choice(request, "state", State.values)
        if state:
            qs = qs.filter(state=state)
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminPvcSerializer(page, many=True, context={"request": request}).data)
        pending = PvcVerification.objects.filter(status=PvcStatus.PENDING)
        response.data["lookup_url"] = get_checker().officer_lookup_url()
        response.data["pending_count"] = pending.count()
        response.data["oldest_pending_at"] = pending.aggregate(t=Min("created_at"))["t"]
        return response


class AdminPvcSettings(_Private):
    def get_permissions(self):
        return [PvcOfficer()] if self.request.method == "GET" else [IsSystemAdmin()]

    def get(self, request):
        return Response(config_payload())

    def patch(self, request):
        services.update_settings(request.user, request.data, request=request)
        return Response(config_payload())


class AdminPvcAction(_Private):
    permission_classes = [PvcOfficer]
    action_name = ""

    def get_throttles(self):
        return [RevealThrottle()] if self.action_name == "reveal" else []

    def post(self, request, pk):
        rec = get_object_or_404(PvcVerification.objects.select_related("user"), pk=pk)
        if self.action_name == "reveal":
            return Response({"vin": services.reveal_vin(rec, request.user, request=request)})
        if self.action_name == "verify":
            from apps.geo.services import chain_ids

            ids = chain_ids(request.data)
            rec = services.verify(rec, request.user, note=str(request.data.get("note", ""))[:300], request=request,
                                  geo=ids if ids["lga"] else None)
        else:
            rec = services.reject(rec, request.user, str(request.data.get("reason", "")), request=request)
        return Response(AdminPvcSerializer(rec, context={"request": request}).data)
