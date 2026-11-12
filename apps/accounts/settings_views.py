"""
Settings API (spec 20 §17). Everything about one person lives under /me/ (private, no-store, rule 8). The public profile
is the only public endpoint; the admin defaults are for system admins.
"""
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as S
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from apps.access.permissions import IsSignedIn, IsSystemAdmin
from apps.core.exceptions import ServiceError

from . import settings_services as svc

# ------------------------------------------------------------------------------------------------ schema shapes
OPTION = inline_serializer("LanguageOption", {"code": S.CharField(), "label": S.CharField(), "native": S.CharField(),
                                              "enabled": S.BooleanField()})
LANGUAGE = inline_serializer("SettingsLanguage", {"language": S.CharField(), "options": OPTION.__class__(many=True)})
DISPLAY = inline_serializer("SettingsAccessibility", {"theme": S.ChoiceField(["system", "light", "dark"]),
                                                      "text_size": S.ChoiceField(["standard", "large", "larger"]),
                                                      "contrast": S.ChoiceField(["system", "standard", "more"]),
                                                      "motion": S.ChoiceField(["system", "reduce"])})
CATEGORY = inline_serializer("NotificationCategory", {"key": S.CharField(), "label": S.CharField(), "kinds": S.ListField(child=S.CharField()),
                                                      "help": S.CharField(), "on": S.BooleanField(), "locked": S.BooleanField(),
                                                      "reason": S.CharField()})
NOTIFICATIONS = inline_serializer("SettingsNotifications", {"categories": CATEGORY.__class__(many=True),
                                                            "candidate_bells_paused": S.BooleanField()})
LGA = inline_serializer("SettingsLga", {"id": S.IntegerField(), "name": S.CharField()})
CONSENT = inline_serializer("SettingsConsent", {"version": S.IntegerField(), "text": S.CharField(), "label": S.CharField()})
PROFILE = inline_serializer("SettingsProfile", {
    "public_profile": S.BooleanField(), "handle": S.CharField(allow_null=True),
    "name_style": S.ChoiceField(["first_initial", "full"]),
    "name_examples": inline_serializer("NameExamples", {"first_initial": S.CharField(), "full": S.CharField()}),
    "bio": S.CharField(), "bio_removed": S.BooleanField(), "photo_removed": S.BooleanField(), "profile_location": S.ChoiceField(["none", "state", "lga"]), "profile_lga": LGA.__class__(allow_null=True),
    "profile_photo": S.BooleanField(), "consent": CONSENT})
PRIVACY = inline_serializer("SettingsPrivacy", {"donation_name_default": S.ChoiceField(["anonymous", "name"]), "verified_badge": S.BooleanField(),
                                                "named_news": S.IntegerField(), "public_profile": S.BooleanField(), "name_style": S.CharField()})
ACCOUNT = inline_serializer("SettingsAccount", {
    "full_name": S.CharField(), "phone_number": S.CharField(), "email": S.CharField(), "created_at": S.DateTimeField(),
    "status": S.CharField(), "home_state": S.CharField(), "membership_status": S.CharField(allow_null=True), "pvc_status": S.CharField(),
    "staff": S.BooleanField(), "mfa_enabled": S.BooleanField(allow_null=True)})
CAMPAIGN = inline_serializer("SettingsCampaign", {"name": S.CharField(), "slug": S.CharField()})
MANAGE = inline_serializer("SettingsManage", {"upcoming_registrations": S.IntegerField(), "pending_volunteer_applications": S.IntegerField()})
SETTINGS = inline_serializer("Settings", {
    "account": ACCOUNT, "profile": PROFILE, "language": LANGUAGE, "accessibility": DISPLAY, "notifications": NOTIFICATIONS,
    "privacy": PRIVACY, "connected": inline_serializer("SettingsConnected", {"campaigns": CAMPAIGN.__class__(many=True)}),
    "manage": MANAGE, "saved_at": S.DateTimeField(allow_null=True), "updated_at": S.DateTimeField(allow_null=True)})
DEVICE = inline_serializer("SettingsDeviceImport", {"accessibility": DISPLAY, "language": LANGUAGE, "saved_at": S.DateTimeField()})
PUBLIC_PROFILE = inline_serializer("PublicProfile", {
    "handle": S.CharField(), "name": S.CharField(), "bio": S.CharField(),
    "location": inline_serializer("PublicProfileLocation", {"state": S.CharField(), "state_label": S.CharField(),
                                                            "lga": S.CharField(allow_null=True)}, allow_null=True),
    "verified_voter": S.BooleanField(), "photo_url": S.CharField(allow_null=True), "member_since": S.CharField(),
    "news": inline_serializer("PublicProfileNews", {"id": S.UUIDField(), "title": S.CharField(), "published_at": S.DateTimeField()},
                              many=True)})
DEFAULTS = inline_serializer("SettingsDefaults", {
    "enabled_languages": S.ListField(child=S.CharField()), "languages": OPTION.__class__(many=True),
    "translations_complete": S.ListField(child=S.CharField()), "notifications": S.DictField(child=S.BooleanField()),
    "notification_labels": S.DictField(child=S.CharField()), "default_theme": S.CharField(), "default_text_size": S.CharField(),
    "updated_at": S.DateTimeField(allow_null=True)})


class SettingsWriteThrottle(UserRateThrottle):
    scope = "settings_write"


class PeopleThrottle(AnonRateThrottle):
    scope = "people_public"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _Private(APIView):
    permission_classes = [IsSignedIn]

    def get_throttles(self):
        return [] if self.request.method == "GET" else [SettingsWriteThrottle()]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


def _data(request) -> dict:
    d = request.data
    if not isinstance(d, dict):
        raise ServiceError("Send a JSON object.", code="validation_error", status_code=400)
    return d


# ---------------------------------------------------------------------------------------------------- signed in
class MySettings(_Private):
    @extend_schema(summary="All your settings (spec 20)", responses=SETTINGS)
    def get(self, request):
        return Response(svc.settings_payload(request.user, request))


class MyProfileSettings(_Private):
    @extend_schema(summary="Name style, bio, location and photo choice for your public name and profile", request=PROFILE,
                   responses=PROFILE)
    def patch(self, request):
        return Response(svc.set_profile(request.user, _data(request)))


class MyPublicProfile(_Private):
    @extend_schema(summary="Turn your public profile on (needs consent)",
                   request=inline_serializer("PublicProfileOn", {"consent": S.BooleanField(), "version": S.IntegerField()}),
                   responses=PROFILE)
    def post(self, request):
        d = _data(request)
        return Response(svc.public_profile_on(request.user, consent=d.get("consent"), version=d.get("version"), request=request))

    @extend_schema(summary="Turn your public profile off", request=None, responses=PROFILE)
    def delete(self, request):
        return Response(svc.public_profile_off(request.user, request=request))


class MyLanguage(_Private):
    @extend_schema(summary="Your interface language", request=inline_serializer("LanguageChoice", {"language": S.CharField()}),
                   responses=LANGUAGE)
    def patch(self, request):
        return Response(svc.set_language(request.user, _data(request).get("language")))


class MyAccessibility(_Private):
    @extend_schema(summary="Theme, text size, contrast and motion", request=DISPLAY, responses=DISPLAY)
    def patch(self, request):
        return Response(svc.set_display(request.user, _data(request)))


class MyNotificationSettings(_Private):
    @extend_schema(summary="Notification categories (essential ones are locked on)", responses=NOTIFICATIONS)
    def get(self, request):
        return Response(svc.notifications_payload(request.user))

    @extend_schema(summary="Turn switchable notification categories on or off, or pause all candidate bells",
                   request=inline_serializer("NotificationSwitches", {k: S.BooleanField(required=False)
                                                                      for k in [*svc.SWITCH_KEYS, "candidate_bells_paused"]}),
                   responses=NOTIFICATIONS)
    def patch(self, request):
        return Response(svc.set_notifications(request.user, _data(request)))


class MyPrivacySettings(_Private):
    @extend_schema(summary="Default name on your donations", request=inline_serializer("PrivacyChoice", {
        "donation_name_default": S.ChoiceField(["anonymous", "name"])}), responses=PRIVACY)
    def patch(self, request):
        return Response(svc.set_privacy(request.user, _data(request)))


class MySignOutOthers(_Private):
    @extend_schema(summary="Sign out of all other devices (needs a recent second check)", request=None,
                   responses=inline_serializer("SignedOut", {"signed_out": S.IntegerField()}))
    def post(self, request):
        return Response(svc.sign_out_others(request.user, request))


class MyImportDevice(_Private):
    @extend_schema(summary="Copy this device's language and display settings to an account that has none yet (once)",
                   request=inline_serializer("DeviceSettings", {"language": S.CharField(required=False), "theme": S.CharField(required=False),
                                                                "text_size": S.CharField(required=False), "contrast": S.CharField(required=False),
                                                                "motion": S.CharField(required=False)}),
                   responses=DEVICE)
    def post(self, request):
        return Response(svc.import_device(request.user, _data(request)))


class MyPause(_Private):
    @extend_schema(summary="Pause your account (needs a recent second check and the word 'pause')",
                   request=inline_serializer("PauseConfirm", {"confirm": S.CharField()}),
                   responses=inline_serializer("Paused", {"paused": S.BooleanField(), "registrations_cancelled": S.IntegerField(),
                                                          "applications_withdrawn": S.IntegerField()}))
    def post(self, request):
        return Response(svc.pause(request.user, confirm=_data(request).get("confirm", ""), request=request))


class MyReactivate(_Private):
    @extend_schema(summary="Reactivate an account you paused", request=None,
                   responses=inline_serializer("Reactivated", {"active": S.BooleanField()}))
    def post(self, request):
        return Response(svc.reactivate(request.user, request=request))


# -------------------------------------------------------------------------------------------------------- public
class PublicProfileView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [PeopleThrottle]

    @extend_schema(summary="A person's public profile (only when they turned it on)", responses=PUBLIC_PROFILE)
    def get(self, request, handle):
        data = svc.public_profile(handle.lower()) if len(handle) == 6 else None
        if data is None:
            raise ServiceError("This profile isn't available.", code="not_found", status_code=404)
        resp = Response(data)
        patch_cache_control(resp, public=True, max_age=300)  # §23.6: a profile turned off can be served for up to 5 minutes
        resp["X-Robots-Tag"] = "noindex"
        return resp

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["X-Robots-Tag"] = "noindex"
        return response


class PublicProfileReport(APIView):
    """§5.1: the bio and photo can be reported through 13 (13's reasons, anyone can report, like news)."""

    permission_classes = []

    def get_throttles(self):
        from apps.news.views import ReportThrottle

        return [ReportThrottle()]

    @extend_schema(summary="Report a public profile's bio or photo (spec 13's reasons)",
                   request=inline_serializer("ProfileReport", {"reason": S.CharField(), "details": S.CharField(required=False),
                                                               "contact_email": S.EmailField(required=False)}),
                   responses={201: inline_serializer("ProfileReportResponse", {"reference": S.CharField(required=False),
                                                                               "route": S.CharField(required=False)})})
    def post(self, request, handle):
        from rest_framework import status

        from apps.audit.services import client_ip
        from apps.moderation import services as moderation
        from apps.moderation import targets as T
        from apps.news.serializers import ReportSerializer

        from .models import UserPreferences

        p = UserPreferences.objects.select_related("user").filter(profile_handle=handle.lower()).first() if len(handle) == 6 else None
        if p is None or not T.is_public("profile", p):
            raise ServiceError("This profile isn't available.", code="not_found", status_code=404)
        ser = ReportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = request.user if request.user and request.user.is_authenticated else None
        out = moderation.report("profile", p, ser.validated_data["reason"], ser.validated_data.get("details", ""), user=user,
                                contact_email="" if user else ser.validated_data.get("contact_email", ""), ip=client_ip(request) or "")
        return Response(out, status=status.HTTP_200_OK if "route" in out else status.HTTP_201_CREATED)


# --------------------------------------------------------------------------------------------------------- admin
class AdminSettingsDefaults(APIView):
    permission_classes = [IsSystemAdmin]

    def get_throttles(self):
        return [] if self.request.method == "GET" else [SettingsWriteThrottle()]

    @extend_schema(summary="Defaults for new accounts and enabled languages", responses=DEFAULTS)
    def get(self, request):
        return Response(svc.defaults_payload())

    @extend_schema(summary="Change defaults for new accounts or enabled languages (audited)", request=DEFAULTS, responses=DEFAULTS)
    def patch(self, request):
        from .language import forget

        out = svc.set_defaults(data=_data(request), actor=request.user, request=request)
        forget()
        return Response(out)
