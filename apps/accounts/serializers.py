from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from apps.access.models import RoleAssignment
from apps.access.services import is_system_admin

from .models import User
from .validators import normalize_ng_phone, validate_full_name


class RoleSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = RoleAssignment
        fields = ["id", "role", "scope_type", "scope_id"]


class MeSerializer(serializers.ModelSerializer):
    """GET /me — the caller's own profile only (spec §6.3)."""

    profile_complete = serializers.BooleanField(read_only=True)
    roles = serializers.SerializerMethodField()
    is_system_admin = serializers.SerializerMethodField()
    pvc_status = serializers.SerializerMethodField()
    admin_areas = serializers.SerializerMethodField()
    adult_confirmed = serializers.SerializerMethodField()
    mfa_enabled = serializers.SerializerMethodField()
    consents_needed = serializers.SerializerMethodField()
    privacy_version = serializers.SerializerMethodField()
    admin_mfa_required = serializers.SerializerMethodField()
    account_status = serializers.SerializerMethodField()
    preferences = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "phone_number", "profile_complete", "roles", "is_system_admin", "pvc_status", "admin_areas",
                  "home_state", "adult_confirmed", "mfa_enabled", "consents_needed", "privacy_version",
                  "admin_mfa_required", "account_status", "preferences"]
        read_only_fields = fields

    def get_pvc_status(self, obj) -> str:
        from apps.pvc.services import status_for

        return status_for(obj)  # the caller's own status only; /me is private (spec 08 §6.2)

    def get_roles(self, obj) -> list:
        return RoleSummarySerializer(obj.role_assignments.filter(is_active=True), many=True).data

    def get_admin_areas(self, obj) -> list:
        from apps.dashboard.services import admin_areas

        return admin_areas(obj)  # spec 11: one place decides which admin areas the header and AdminNav show

    def get_is_system_admin(self, obj) -> bool:
        return is_system_admin(obj)

    # spec 14
    def get_adult_confirmed(self, obj) -> bool:
        return obj.adult_confirmed_at is not None

    def get_mfa_enabled(self, obj):
        from apps.access.security import mfa_claim_present, mfa_enabled

        claims = getattr(self.context.get("auth"), "claims", None)
        if claims is None or not mfa_claim_present(claims):
            return obj.mfa_enabled
        return mfa_enabled(claims)

    def get_consents_needed(self, obj) -> list:
        from apps.privacy.consent import consents_needed

        return consents_needed(obj)

    def get_admin_mfa_required(self, obj) -> bool:
        """Spec 14 §4.6: the admin pages show the two-step gate only when the server would refuse."""
        from django.conf import settings

        return bool(settings.ADMIN_MFA_REQUIRED)

    @extend_schema_field(serializers.ChoiceField(["active", "paused"]))
    def get_account_status(self, obj) -> str:
        """Spec 20 §12: "active", or "paused" when the person paused their own account (they can still read /me/)."""
        return "active" if obj.is_active else "paused"

    @extend_schema_field(inline_serializer("MePreferences", {
        "language": serializers.CharField(), "theme": serializers.CharField(), "text_size": serializers.CharField(),
        "contrast": serializers.CharField(), "motion": serializers.CharField(), "saved_at": serializers.DateTimeField(allow_null=True),
        "updated_at": serializers.DateTimeField(allow_null=True)}))
    def get_preferences(self, obj):
        """Spec 20 §13: applied by the app after sign-in and written to the device for the next first paint."""
        from .settings_services import display_payload, prefs_for

        p = prefs_for(obj)
        return {"language": p.language, **display_payload(p), "saved_at": p.saved_at, "updated_at": p.updated_at}

    def get_privacy_version(self, obj) -> int:
        from apps.privacy.register import PRIVACY_VERSION

        return PRIVACY_VERSION


class ProfileUpdateSerializer(serializers.Serializer):
    """PATCH /me — only full_name and phone_number are writable. Anything else is ignored."""

    full_name = serializers.CharField(
        required=False, allow_blank=True, max_length=200,
        error_messages={"blank": "Enter your first and last name."},
    )
    phone_number = serializers.CharField(
        required=False, allow_blank=True, max_length=25,
        error_messages={"blank": "Enter your phone number."},
    )
    home_state = serializers.CharField(required=False, allow_blank=True, max_length=20)  # spec 12 §4.5
    adult_confirmed = serializers.BooleanField(required=False)  # spec 14 §4.3; only `true` does anything

    def validate_adult_confirmed(self, value):
        if value is not True:
            raise serializers.ValidationError("NDCConnect is for voters: you need to be 18 or older.")
        return value

    def validate_home_state(self, value):
        from apps.candidates.reference import State

        if value and value not in State.values:
            raise serializers.ValidationError("Choose a state from the list.")
        return value

    def validate_full_name(self, value):
        try:
            return validate_full_name(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages[0]) from exc

    def validate_phone_number(self, value):
        try:
            return normalize_ng_phone(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages[0]) from exc
