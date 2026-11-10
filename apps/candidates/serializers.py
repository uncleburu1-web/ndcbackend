from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from apps.accounts.models import User

from . import photos
from .models import Candidate
from .reference import Position, State


def _photo(obj, request):
    if not obj.photo_800:
        return None
    from apps.media.services import srcset

    return {"url_800": photos.public_url(request, obj.photo_800), "url_200": photos.public_url(request, obj.photo_200),
            "srcset": srcset(obj.photo_800, "square", request, small=obj.photo_200)}


class PublicCandidateSerializer(serializers.ModelSerializer):
    """§7.1 — public fields ONLY. Never notes, verifier, linked accounts, creator, status history."""

    position_label = serializers.CharField(source="get_position_display", read_only=True)
    area_display = serializers.CharField(read_only=True)
    photo = serializers.SerializerMethodField()
    verified = serializers.SerializerMethodField()

    class Meta:
        model = Candidate
        fields = ["slug", "full_name", "party", "position", "position_label", "election_year", "state", "area_display",
                  "biography", "campaign_summary", "priorities", "links", "photo", "verified", "published_at"]
        read_only_fields = fields

    def get_photo(self, obj) -> dict | None:
        return _photo(obj, self.context.get("request"))

    def get_verified(self, obj) -> bool:
        return obj.is_verified


class PublicCandidateListSerializer(PublicCandidateSerializer):
    """List view: the card needs less than the profile."""

    class Meta(PublicCandidateSerializer.Meta):
        fields = ["slug", "full_name", "party", "position", "position_label", "election_year", "state", "area_display",
                  "photo", "verified"]
        read_only_fields = fields


class LinkSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=40)
    url = serializers.CharField(max_length=500)


class CandidateWriteSerializer(serializers.Serializer):
    """Admin create/edit input. Model.clean() applies the area/priority/link rules."""

    full_name = serializers.CharField(max_length=150)
    party = serializers.CharField(max_length=40, required=False)
    position = serializers.ChoiceField(choices=Position.choices)
    election_year = serializers.IntegerField()
    state = serializers.ChoiceField(choices=State.choices, required=False, allow_blank=True)
    area_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    ward_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    # spec 19 §5.2: chosen from the lists; area_name and ward_name are then written from the chosen names
    constituency_id = serializers.IntegerField(required=False, allow_null=True, help_text="Senators, House of Reps, State Assembly")
    lga_id = serializers.IntegerField(required=False, allow_null=True, help_text="LGA chairmen and councillors")
    ward_id = serializers.IntegerField(required=False, allow_null=True, help_text="Councillors")
    biography = serializers.CharField(max_length=5000, required=False, allow_blank=True, trim_whitespace=False)
    campaign_summary = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    priorities = serializers.ListField(child=serializers.CharField(max_length=500, allow_blank=True), required=False)
    links = LinkSerializer(many=True, required=False)

    def validate_biography(self, value):
        return value.strip()


class AdminCandidateSerializer(serializers.ModelSerializer):
    """Admin read view — everything, including internal fields."""

    position_label = serializers.CharField(source="get_position_display", read_only=True)
    area_display = serializers.CharField(read_only=True)
    photo = serializers.SerializerMethodField()
    is_verified = serializers.BooleanField(read_only=True)
    verified_by_email = serializers.EmailField(source="verified_by.email", read_only=True, default=None)
    constituency = serializers.SerializerMethodField()
    lga_id = serializers.IntegerField(source="geo_lga_id", read_only=True, allow_null=True)
    ward_id = serializers.IntegerField(source="geo_ward_id", read_only=True, allow_null=True)

    class Meta:
        model = Candidate
        fields = ["id", "slug", "full_name", "party", "position", "position_label", "election_year", "state",
                  "area_name", "ward_name", "area_display", "constituency", "lga_id", "ward_id", "biography", "campaign_summary", "priorities", "links",
                  "photo", "status", "is_verified", "verified_at", "verified_by_email", "verification_notes",
                  "published_at", "created_at", "updated_at"]
        read_only_fields = fields

    def get_photo(self, obj) -> dict | None:
        return _photo(obj, self.context.get("request"))

    @extend_schema_field(inline_serializer("CandidateConstituency", {"id": serializers.IntegerField(), "code": serializers.CharField(),
                                                                      "name": serializers.CharField(), "kind": serializers.CharField()},
                                           allow_null=True))
    def get_constituency(self, obj):
        c = obj.geo_constituency
        return {"id": c.pk, "code": c.code, "name": c.name, "kind": c.kind} if c else None


class LinkedAccountSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(source="user.id")
    email = serializers.EmailField(source="user.email")
    full_name = serializers.CharField(source="user.full_name")
    linked_at = serializers.DateTimeField(source="created_at")


class LinkAccountSerializer(serializers.Serializer):
    email = serializers.EmailField(error_messages={"required": "Enter the account's email.", "invalid": "Enter a valid email address."})

    def validate_email(self, value):
        matches = list(User.objects.filter(email__iexact=value.strip(), is_active=True)[:2])
        if not matches:
            raise serializers.ValidationError("No active NDCConnect account uses that email. They need to sign up first.")
        if len(matches) > 1:
            raise serializers.ValidationError("More than one account uses that email. Contact support to resolve it.")
        return matches[0]


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class NotesSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, max_length=2000)
