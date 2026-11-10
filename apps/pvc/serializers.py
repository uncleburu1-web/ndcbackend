from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.candidates.reference import State

from .models import PvcVerification


class PvcSubmitSerializer(serializers.Serializer):
    vin = serializers.CharField(max_length=40, error_messages={"blank": "Enter your VIN.", "required": "Enter your VIN."})
    surname = serializers.CharField(max_length=80, error_messages={"blank": "Enter your surname as printed on the card."})
    state = serializers.ChoiceField(choices=State.choices, error_messages={"invalid_choice": "Choose the state you registered in."})
    lga = serializers.CharField(max_length=120, required=False, allow_blank=True)  # typed, or filled from lga_id (spec 19 §7.2)
    polling_unit = serializers.CharField(max_length=160, required=False, allow_blank=True)
    lga_id = serializers.IntegerField(required=False, allow_null=True)
    ward_id = serializers.IntegerField(required=False, allow_null=True)
    polling_unit_id = serializers.IntegerField(required=False, allow_null=True)
    geo_not_listed = serializers.BooleanField(required=False, default=False, help_text="\"My place isn't listed\": keep the text, review it")
    consent = serializers.BooleanField()

    def validate_consent(self, v):
        if v is not True:
            raise serializers.ValidationError("Tick this so we can check your details on INEC's register.")
        return v


class GeoPlaceItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    code = serializers.CharField()
    active = serializers.BooleanField()


class GeoPlaceSerializer(serializers.Serializer):
    """Spec 19: a record's chosen place (the person's own view, or an officer's existing record view)."""
    lga = GeoPlaceItemSerializer(allow_null=True)
    ward = GeoPlaceItemSerializer(allow_null=True)
    polling_unit = GeoPlaceItemSerializer(allow_null=True)


class MyPvcSerializer(serializers.ModelSerializer):
    """The user's own view — never the full VIN (spec 06 §3.9)."""

    vin_masked = serializers.SerializerMethodField()
    state_label = serializers.CharField(source="get_state_display")
    place = serializers.SerializerMethodField()
    pending_place = serializers.SerializerMethodField()

    class Meta:
        model = PvcVerification
        fields = ["status", "vin_masked", "surname", "state", "state_label", "lga", "polling_unit", "reason", "reviewed_at", "created_at",
                  "updated_at", "show_badge", "geo_source", "place", "pending_place"]

    @extend_schema_field(GeoPlaceSerializer)
    def get_place(self, obj):
        from apps.geo.records import place

        return place(obj.geo_lga, obj.geo_ward, obj.geo_polling_unit)

    @extend_schema_field(GeoPlaceSerializer(allow_null=True))
    def get_pending_place(self, obj):
        from apps.geo.records import place

        return place(obj.pending_geo_lga, obj.pending_geo_ward, obj.pending_geo_polling_unit) if obj.pending_geo_lga_id else None

    def get_vin_masked(self, obj):
        return f"•••• {obj.vin_last4}" if obj.vin_last4 else ""


class AdminPvcSerializer(MyPvcSerializer):
    id = serializers.UUIDField()
    user_name = serializers.CharField(source="user.full_name")
    user_email = serializers.EmailField(source="user.email")
    reviewed_by_email = serializers.EmailField(source="reviewed_by.email", default=None)
    is_own = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()
    suggestion = serializers.SerializerMethodField()

    class Meta(MyPvcSerializer.Meta):
        fields = ["id", "user_name", "user_email", "reviewed_by_email", "is_own", "events", "suggestion"] + MyPvcSerializer.Meta.fields

    @extend_schema_field(serializers.JSONField())
    def get_suggestion(self, obj):
        """Spec 19 §8.2: the decision screen's picker is pre-filled with the link, else the best match for the typed text."""
        if obj.status != "pending":
            return None
        from apps.geo.services import suggest

        return suggest(obj.state, obj.lga, "", obj.polling_unit)

    def get_is_own(self, obj):
        request = self.context.get("request")
        return bool(request and obj.user_id == request.user.pk)

    def get_events(self, obj):
        from .models import PvcEvent

        return [{"kind": e.kind, "note": e.note, "created_at": e.created_at, "actor_email": e.actor.email if e.actor else None}
                for e in PvcEvent.objects.filter(user_id=obj.user_id).select_related("actor").order_by("created_at")]
