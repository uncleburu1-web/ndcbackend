"""PVC verification API — spec 06 §7.1–7.2."""
from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers, patch_cache_control
from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.models import Role
from apps.access.permissions import HasRole, IsSignedIn

from . import services
from .checkers import get_checker
from .models import PvcStatus, PvcVerification
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
        data = MyPvcSerializer(rec).data if rec else {"status": "unverified"}
        data["inec_pvc_url"] = settings.INEC_PVC_URL or None
        return Response(data)

    def put(self, request):
        ser = PvcSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        v = ser.validated_data
        rec = services.submit(request.user, vin=v["vin"], surname=v["surname"], state=v["state"], lga=v["lga"],
                              polling_unit=v.get("polling_unit", ""), request=request)
        return Response(MyPvcSerializer(rec).data, status=status.HTTP_200_OK)

    def delete(self, request):
        services.withdraw(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


OfficerOnly = HasRole(Role.MEMBERSHIP_OFFICER)


class AdminPvcList(_Private):
    permission_classes = [OfficerOnly]

    def get(self, request):
        qs = PvcVerification.objects.select_related("user", "reviewed_by").order_by("created_at")
        st = request.query_params.get("status", "pending")
        if st in PvcStatus.values:
            qs = qs.filter(status=st)
        paginator = Page20()
        page = paginator.paginate_queryset(qs, request, view=self)
        response = paginator.get_paginated_response(AdminPvcSerializer(page, many=True).data)
        response.data["lookup_url"] = get_checker().officer_lookup_url()
        response.data["pending_count"] = PvcVerification.objects.filter(status=PvcStatus.PENDING).count()
        return response


class AdminPvcAction(_Private):
    permission_classes = [OfficerOnly]
    action_name = ""

    def get_throttles(self):
        return [RevealThrottle()] if self.action_name == "reveal" else []

    def post(self, request, pk):
        rec = get_object_or_404(PvcVerification.objects.select_related("user"), pk=pk)
        if self.action_name == "reveal":
            return Response({"vin": services.reveal_vin(rec, request.user, request=request)})
        if self.action_name == "verify":
            rec = services.verify(rec, request.user, note=str(request.data.get("note", ""))[:300], request=request)
        else:
            rec = services.reject(rec, request.user, str(request.data.get("reason", "")), request=request)
        return Response(AdminPvcSerializer(rec).data)
