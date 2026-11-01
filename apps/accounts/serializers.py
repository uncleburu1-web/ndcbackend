from django.core.exceptions import ValidationError as DjangoValidationError
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

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "phone_number", "profile_complete", "roles", "is_system_admin", "pvc_status"]
        read_only_fields = fields

    def get_pvc_status(self, obj) -> str:
        from apps.pvc.services import status_for

        return status_for(obj)  # the caller's own status only; /me is private (spec 08 §6.2)

    def get_roles(self, obj) -> list:
        return RoleSummarySerializer(obj.role_assignments.filter(is_active=True), many=True).data

    def get_is_system_admin(self, obj) -> bool:
        return is_system_admin(obj)


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
