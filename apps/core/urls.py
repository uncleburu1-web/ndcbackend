from django.db import connection
from django.urls import path
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def health(request):
    """Liveness + DB check (spec §6.3)."""
    try:
        connection.ensure_connection()
        db = "ok"
    except Exception:  # pragma: no cover
        db = "unavailable"
    return Response({"status": "ok" if db == "ok" else "degraded", "database": db}, status=200 if db == "ok" else 503)


urlpatterns = [path("health/", health, name="health")]
