import os

from django.db import connection
from django.urls import path
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

HEALTH_SCHEMA = inline_serializer("Health", {"status": serializers.ChoiceField(["ok", "degraded"]),
                                             "database": serializers.ChoiceField(["ok", "unavailable"]),
                                             "api_version": serializers.CharField(), "commit": serializers.CharField()})
API_VERSION = "v1"


def _commit() -> str:
    """The deployed commit, for Settings → About (spec 20 §15). Railway sets RAILWAY_GIT_COMMIT_SHA; it isn't a secret."""
    return (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or "")[:7]
READY_SCHEMA = OpenApiResponse(inline_serializer("Ready", {
    "status": serializers.ChoiceField(["ready", "not_ready"]),
    "checks": serializers.DictField(child=serializers.ChoiceField(["ok", "failed"]))}))


@extend_schema(summary="Liveness: the process is up and the database answers", tags=["health"], auth=[],
               responses={200: HEALTH_SCHEMA, 503: HEALTH_SCHEMA})
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
    return Response({"status": "ok" if db == "ok" else "degraded", "database": db, "api_version": API_VERSION, "commit": _commit()},
                    status=200 if db == "ok" else 503)


class ReadyThrottle(AnonRateThrottle):
    scope = "ready"


@extend_schema(summary="Readiness for the load balancer", tags=["health"], auth=[],
               responses={200: READY_SCHEMA, 503: READY_SCHEMA})
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([ReadyThrottle])
def ready(request):
    """Spec 15 §12.5: database, migrations, shared cache, media backend, outbox. Names and ok/failed only."""
    from .ready import run

    ok, checks = run()
    resp = Response({"status": "ready" if ok else "not_ready", "checks": checks}, status=200 if ok else 503)
    resp["Cache-Control"] = "no-store"
    return resp


urlpatterns = [path("health/", health, name="health"), path("ready/", ready, name="ready")]
