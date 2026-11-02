"""GET /api/v1/admin/overview/ (spec 11 §7). Any admin role; the payload holds only that role's sections."""
from rest_framework.response import Response

from apps.access.permissions import IsSignedIn
from apps.access.views import AdminReadThrottle, AdminView

from . import services


class IsAnyAdmin(IsSignedIn):
    message = "You don't have access to the admin."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and bool(services.admin_roles(request.user))


class Overview(AdminView):
    permission_classes = [IsAnyAdmin]
    throttle_classes = [AdminReadThrottle]

    def get(self, request):
        return Response(services.overview(request.user))
