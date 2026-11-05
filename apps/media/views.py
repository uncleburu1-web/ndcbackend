"""POST /api/v1/media/upload-signature/ (spec 15 §7.3)."""
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import OpenApiExample, extend_schema, inline_serializer
from rest_framework import serializers as S
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn
from apps.core.exceptions import ServiceError

from . import services
from .models import Purpose


class SignatureThrottle(UserRateThrottle):
    scope = "media_sign"


SIGN_REQUEST = inline_serializer("UploadSignatureRequest", {
    "purpose": S.ChoiceField(Purpose.values),  # every image purpose (event_image: spec 18)
    "target_id": S.CharField(help_text="The candidate, post or article the image is for.")})
SIGN_RESPONSE = inline_serializer("UploadSignature", {
    "mode": S.ChoiceField(["direct", "server"], help_text="server: send the file to the image endpoint as multipart instead."),
    "upload_url": S.CharField(required=False), "cloud_name": S.CharField(required=False), "api_key": S.CharField(required=False),
    "timestamp": S.IntegerField(required=False), "public_id": S.CharField(required=False), "upload_preset": S.CharField(required=False),
    "allowed_formats": S.CharField(required=False), "signature": S.CharField(required=False), "max_bytes": S.IntegerField(required=False)})


class UploadSignature(APIView):
    """Spec 15 §7.3: signed parameters for one direct upload to Cloudinary, for a purpose and target you may change.

    The API secret never leaves the server. Returns {"mode": "server"} when images are stored locally."""

    permission_classes = [IsSignedIn]
    throttle_classes = [SignatureThrottle]

    @extend_schema(summary="Get signed parameters for one image upload", request=SIGN_REQUEST, responses=SIGN_RESPONSE,
                   examples=[OpenApiExample("Request", request_only=True, value={"purpose": "candidate_photo", "target_id": "8b1f…"}),
                             OpenApiExample("Local storage", response_only=True, value={"mode": "server"})])
    def post(self, request):
        d = request.data or {}
        try:
            out = services.signature_for(request.user, str(d.get("purpose", "")), d.get("target_id"))
        except services.MediaUnavailable:
            raise ServiceError("Image upload isn't working right now.", code="media_unavailable", status_code=503) from None
        resp = Response(out)
        add_never_cache_headers(resp)
        patch_cache_control(resp, private=True, no_store=True)
        return resp
