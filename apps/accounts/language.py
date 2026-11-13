"""
The language a request should be answered in (spec 20 §6, 21 §6): `request.ndc_language`, activated for Django's
gettext so error messages and field errors come back translated; `Content-Language` on every response.

Guests: the best enabled match in Accept-Language. Signed-in: their saved language (if still enabled).
"""
from django.core.cache import cache

CACHE_KEY = "settings:enabled-languages"


def enabled() -> list[str]:
    hit = cache.get(CACHE_KEY)
    if hit is None:
        from .settings_services import enabled_languages

        hit = enabled_languages()
        cache.set(CACHE_KEY, hit, 60)
    return hit


def forget():
    cache.delete(CACHE_KEY)


def from_header(header: str) -> str:
    """Best enabled language in an Accept-Language header ("ha-NG,ha;q=0.9,en;q=0.8"), else English."""
    on = enabled()
    best, best_q = "en", -1.0
    for part in (header or "").split(","):
        bits = part.strip().split(";")
        tag = bits[0].strip().lower()
        if not tag:
            continue
        q = 1.0
        for b in bits[1:]:
            if b.strip().startswith("q="):
                try:
                    q = float(b.strip()[2:])
                except ValueError:
                    q = 0.0
        base = tag.split("-")[0]
        code = tag if tag in on else base if base in on else None
        if code and q > best_q:
            best, best_q = code, q
    return best


class LanguageMiddleware:
    """Sets request.ndc_language for every request (guests); authentication refines it for signed-in people."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.utils import translation

        request.ndc_language = from_header(request.META.get("HTTP_ACCEPT_LANGUAGE", ""))
        translation.activate(request.ndc_language)  # spec 21 §6; authentication may switch it to the account's language
        try:
            response = self.get_response(request)
        finally:
            lang = getattr(request, "ndc_language", "en")
            translation.deactivate()
        if response is not None and not response.has_header("Content-Language"):
            response["Content-Language"] = lang
        return response


def attach_account_language(request, user):
    from .models import UserPreferences

    lang = UserPreferences.objects.filter(user=user).values_list("language", flat=True).first()
    if lang and lang in enabled():
        from django.utils import translation

        request._request.ndc_language = lang
        translation.activate(lang)
