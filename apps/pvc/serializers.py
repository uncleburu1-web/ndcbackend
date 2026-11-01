from rest_framework import serializers

from apps.candidates.reference import State

from .models import PvcVerification


class PvcSubmitSerializer(serializers.Serializer):
    vin = serializers.CharField(max_length=40, error_messages={"blank": "Enter your VIN.", "required": "Enter your VIN."})
    surname = serializers.CharField(max_length=80, error_messages={"blank": "Enter your surname as printed on the card."})
    state = serializers.ChoiceField(choices=State.choices, error_messages={"invalid_choice": "Choose the state you registered in."})
    lga = serializers.CharField(max_length=120, error_messages={"blank": "Enter the LGA you registered in."})
    polling_unit = serializers.CharField(max_length=160, required=False, allow_blank=True)
    consent = serializers.BooleanField()

    def validate_consent(self, v):
        if v is not True:
            raise serializers.ValidationError("Tick this so we can check your details on INEC's register.")
        return v


class MyPvcSerializer(serializers.ModelSerializer):
    """The user's own view — never the full VIN (spec 06 §3.9)."""

    vin_masked = serializers.SerializerMethodField()
    state_label = serializers.CharField(source="get_state_display")

    class Meta:
        model = PvcVerification
        fields = ["status", "vin_masked", "surname", "state", "state_label", "lga", "polling_unit", "reason", "reviewed_at", "created_at",
                  "updated_at", "show_badge"]

    def get_vin_masked(self, obj):
        return f"•••• {obj.vin_last4}" if obj.vin_last4 else ""


class AdminPvcSerializer(MyPvcSerializer):
    id = serializers.UUIDField()
    user_name = serializers.CharField(source="user.full_name")
    user_email = serializers.EmailField(source="user.email")
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", default=None)
    is_own = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()

    class Meta(MyPvcSerializer.Meta):
        fields = ["id", "user_name", "user_email", "reviewed_by_email", "is_own", "events"] + MyPvcSerializer.Meta.fields

    def get_is_own(self, obj):
        request = self.context.get("request")
        return bool(request and obj.user_id == request.user.pk)

    def get_events(self, obj):
        from .models import PvcEvent

        return [{"kind": e.kind, "note": e.note, "created_at": e.created_at, "actor_email": e.actor.email if e.actor else None}
                for e in PvcEvent.objects.filter(user_id=obj.user_id).select_related("actor").order_by("created_at")]
