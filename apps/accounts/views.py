from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn

from . import services
from .serializers import MeSerializer, ProfileUpdateSerializer


class ProfileWriteThrottle(SimpleRateThrottle):
    """Per-IP limit on profile writes (spec §8)."""

    scope = "profile_write"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class MeView(APIView):
    """GET / PATCH /api/v1/me/ — spec §6.3."""

    permission_classes = [IsSignedIn]

    def get_throttles(self):
        return [ProfileWriteThrottle()] if self.request.method == "PATCH" else []

    def get(self, request):
        return Response(MeSerializer(request.user, context={"auth": request.auth}).data)

    def patch(self, request):
        ser = ProfileUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        just_completed = services.update_profile(request.user, **ser.validated_data, request=request)
        request.user.refresh_from_db()
        data = MeSerializer(request.user, context={"auth": request.auth}).data
        if just_completed:
            data["just_completed"] = True
        return Response(data, status=status.HTTP_200_OK)
