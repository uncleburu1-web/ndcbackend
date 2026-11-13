"""
Spec 21 §13–§14 and §9: the public list of enabled languages, the admin Languages screen (completeness, review
sign-offs), and the languages a moderator or news reviewer reads.
"""
from django.db import transaction
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as S
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import i18n
from apps.core.exceptions import ServiceError

from .models import LanguageReview, ReviewScope
from .settings_services import enabled_languages, ready_languages

LANGUAGES = inline_serializer("Languages", {
    "languages": inline_serializer("Language", {"code": S.CharField(), "native": S.CharField(), "english": S.CharField()}, many=True),
    "legal_version": S.CharField()})


class LanguagesThrottle(AnonRateThrottle):
    scope = "people_public"


class PublicLanguages(APIView):
    """GET /languages/ — the enabled interface languages, for guests' switch (§13). Public, cached 5 minutes."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LanguagesThrottle]

    @extend_schema(summary="Enabled interface languages (spec 21 §13)", responses=LANGUAGES)
    def get(self, request):
        langs = [{"code": c, "native": i18n.NATIVE[c], "english": i18n.ENGLISH_NAME[c]} for c in enabled_languages()]
        resp = Response({"languages": langs, "legal_version": i18n.legal_version()})
        patch_cache_control(resp, public=True, max_age=300)
        return resp


def _review_payload(r: LanguageReview) -> dict:
    return {"id": str(r.pk), "language": r.language, "scope": r.scope, "catalogue_version": r.catalogue_version,
            "interface_percent": r.interface_percent, "backend_percent": r.backend_percent, "reviewer_name": r.reviewer_name,
            "note": r.note, "recorded_by": getattr(r.recorded_by, "full_name", "") or "", "created_at": r.created_at}


def admin_payload() -> dict:
    on, ready, legal = set(enabled_languages()), ready_languages(), i18n.legal_version()
    rows = []
    for code in i18n.LANGUAGES:
        reviews = list(LanguageReview.objects.filter(language=code).select_related("recorded_by")[:20]) if code != "en" else []
        interface = next((r for r in reviews if r.scope == ReviewScope.INTERFACE), None)
        legal_review = next((r for r in reviews if r.scope == ReviewScope.LEGAL and r.catalogue_version == legal), None)
        rows.append({"code": code, "native": i18n.NATIVE[code], "english": i18n.ENGLISH_NAME[code], "enabled": code in on,
                     "ready": code in ready, "backend": i18n.stats(code),
                     "last_interface_review": _review_payload(interface) if interface else None,
                     "legal_reviewed": code == "en" or legal_review is not None,
                     "reviews": [_review_payload(r) for r in reviews]})
    return {"languages": rows, "legal_version": legal}


class _NoStore(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        return response


class AdminLanguages(_NoStore):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="Translation completeness and review sign-offs per language (spec 21 §14)",
                   responses=inline_serializer("AdminLanguages", {"languages": S.ListField(child=S.DictField()),
                                                                  "legal_version": S.CharField()}))
    def get(self, request):
        return Response(admin_payload())


class ReviewInput(S.Serializer):
    scope = S.ChoiceField(ReviewScope.choices)
    reviewer_name = S.CharField(max_length=120)
    catalogue_version = S.CharField(max_length=40, required=False, allow_blank=True,
                                    help_text="Interface: the web app's catalogue version (completeness.json). Legal: ignored.")
    interface_percent = S.IntegerField(min_value=0, max_value=100, required=False, default=0,
                                       help_text="The web app's completeness for this language when reviewed")
    note = S.CharField(max_length=500, required=False, allow_blank=True)


class AdminLanguageReviews(_NoStore):
    permission_classes = [IsSystemAdmin]

    @extend_schema(summary="Record a native speaker's (or lawyer's) review of a language (audited)", request=ReviewInput,
                   responses=inline_serializer("LanguageReviewOut", {"id": S.UUIDField()}))
    def post(self, request, code):
        if code not in i18n.TRANSLATED:
            raise ServiceError("Choose Hausa, Igbo, Yoruba or Nigerian Pidgin.", code="language_invalid", status_code=400)
        ser = ReviewInput(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        legal = d["scope"] == ReviewScope.LEGAL
        version = i18n.legal_version() if legal else (d.get("catalogue_version") or "").strip()
        if not legal and not version:
            raise ServiceError("Give the catalogue version the reviewer checked.", code="validation_error", status_code=400)
        r = LanguageReview.objects.create(
            language=code, scope=d["scope"], catalogue_version=version, interface_percent=0 if legal else d.get("interface_percent", 0),
            backend_percent=i18n.stats(code)["percent"], reviewer_name=d["reviewer_name"].strip(), note=d.get("note", ""),
            recorded_by=request.user)
        record(AuditLog.Action.LANGUAGE_REVIEW_RECORDED, target=r, actor=request.user, request=request, language=code, scope=r.scope,
               version=version)
        return Response(_review_payload(r), status=201)


class IsStaff(BasePermission):
    """Moderators, community moderators and news reviewers (and system admins) say which languages they read."""

    def has_permission(self, request, view):
        from apps.access.models import Role, RoleAssignment
        from apps.access.services import is_system_admin

        u = request.user
        return bool(u and u.is_authenticated and (is_system_admin(u) or RoleAssignment.objects.filter(
            user=u, role__in=[Role.MODERATOR, Role.NEWS_REVIEWER], is_active=True).exists()))


class MyStaffLanguages(_NoStore):
    permission_classes = [IsSignedIn, IsStaff]
    SCHEMA = inline_serializer("StaffLanguages", {"languages": S.ListField(child=S.CharField())})

    @extend_schema(summary="The languages I read, for my review queues (spec 21 §9)", responses=SCHEMA)
    def get(self, request):
        from apps.moderation.models import StaffLanguage

        return Response({"languages": sorted(StaffLanguage.objects.filter(user=request.user).values_list("language", flat=True))})

    @extend_schema(summary="Set the languages I read", request=SCHEMA, responses=SCHEMA)
    def put(self, request):
        from apps.moderation.models import StaffLanguage

        langs = request.data.get("languages")
        if not isinstance(langs, list) or any(c not in i18n.LANGUAGES for c in langs):
            raise ServiceError("Choose languages from the list.", code="language_invalid", status_code=400)
        with transaction.atomic():
            StaffLanguage.objects.filter(user=request.user).exclude(language__in=langs).delete()
            for c in set(langs):
                StaffLanguage.objects.get_or_create(user=request.user, language=c)
        return self.get(request)
