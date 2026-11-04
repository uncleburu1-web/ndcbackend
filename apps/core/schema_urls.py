"""GET /api/v1/schema/ (spec 15 §14.2): system admins in production, anyone in development.
Swagger UI and ReDoc only in development (they load assets the API's CSP blocks)."""
from django.conf import settings
from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer

from apps.access.permissions import IsSystemAdmin


class SchemaView(SpectacularAPIView):
    """The live OpenAPI schema. Internal: system admins in production."""

    renderer_classes = [JSONRenderer]

    def get_permissions(self):
        return [AllowAny()] if settings.DEBUG else [IsSystemAdmin()]

    def get_authenticators(self):
        return [] if settings.DEBUG else super().get_authenticators()


urlpatterns = [path("schema/", SchemaView.as_view(), name="schema")]
if settings.DEBUG:
    urlpatterns += [
        path("schema/docs/", SpectacularSwaggerView.as_view(url_name="schema", authentication_classes=[], permission_classes=[AllowAny]),
             name="schema-docs"),
        path("schema/redoc/", SpectacularRedocView.as_view(url_name="schema", authentication_classes=[], permission_classes=[AllowAny]),
             name="schema-redoc"),
    ]
