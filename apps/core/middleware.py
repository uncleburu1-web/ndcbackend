"""API response headers (spec 14 §4.9). The web app's own headers live in ndcfrontend/deploy/."""
from django.conf import settings

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=()"


class SecurityHeaders:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        response.setdefault("Cross-Origin-Resource-Policy", getattr(settings, "CROSS_ORIGIN_RESOURCE_POLICY", "same-site"))
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response
