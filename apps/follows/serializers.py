from rest_framework import serializers

from apps.candidates.serializers import PublicCandidateListSerializer

from .models import CandidateFollow


class FollowSerializer(serializers.ModelSerializer):
    """§7.3 — candidate fields come from 03's public list serializer, so nothing private can leak."""

    candidate = PublicCandidateListSerializer(read_only=True)
    followed_at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = CandidateFollow
        fields = ["candidate", "notify", "followed_at"]
        read_only_fields = fields


class NotifySerializer(serializers.Serializer):
    notify = serializers.BooleanField(error_messages={
        "required": "Say whether notifications should be on or off.",
        "invalid": "Notifications must be true or false.",
    })

    def to_internal_value(self, data):
        # A JSON boolean only — DRF's BooleanField would also accept "yes", 1, "on" (§9).
        value = data.get("notify") if isinstance(data, dict) else None
        if "notify" in (data or {}) and not isinstance(value, bool):
            raise serializers.ValidationError({"notify": ["Notifications must be true or false."]})
        return super().to_internal_value(data)
