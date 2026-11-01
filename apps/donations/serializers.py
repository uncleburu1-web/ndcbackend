from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.accounts.validators import normalize_ng_phone
from apps.candidates.reference import State

from .models import Designation, DisplayChoice, Donation

REQUIRED = "This is required."


class DeclarationsSerializer(serializers.Serializer):
    own_funds = serializers.BooleanField()
    nigerian_source = serializers.BooleanField()
    voluntary = serializers.BooleanField()

    def validate(self, attrs):
        missing = {k: ["Tick this to continue."] for k in ("own_funds", "nigerian_source", "voluntary") if attrs.get(k) is not True}
        if missing:
            raise serializers.ValidationError(missing)
        return attrs


class StartDonationSerializer(serializers.Serializer):
    amount_kobo = serializers.IntegerField(min_value=1, error_messages={"invalid": "Enter a whole naira amount."})
    designation = serializers.ChoiceField(choices=Designation.choices)
    candidate_slug = serializers.CharField(required=False, allow_blank=True, max_length=80)
    donor_name = serializers.CharField(max_length=150, error_messages={"required": REQUIRED, "blank": "Enter your full name."})
    donor_email = serializers.EmailField(error_messages={"required": REQUIRED, "invalid": "Enter a valid email address."})
    donor_phone = serializers.CharField(max_length=24, error_messages={"required": REQUIRED, "blank": "Enter your phone number."})
    donor_state = serializers.ChoiceField(choices=State.choices, error_messages={"required": REQUIRED, "invalid_choice": "Choose your state."})
    donor_address = serializers.CharField(max_length=300, error_messages={"required": REQUIRED, "blank": "Enter your address."})
    display_choice = serializers.ChoiceField(choices=[DisplayChoice.NAME, DisplayChoice.ANONYMOUS], required=False)
    declarations = DeclarationsSerializer()

    def validate_donor_name(self, v):
        v = " ".join(v.split())
        if len(v) < 3:
            raise serializers.ValidationError("Enter your full name.")
        return v

    def validate_donor_phone(self, v):
        try:
            return normalize_ng_phone(v)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages) from None

    def validate(self, attrs):
        if attrs["designation"] == Designation.CANDIDATE and not attrs.get("candidate_slug"):
            raise serializers.ValidationError({"candidate_slug": ["Choose a candidate."]})
        return attrs


def _candidate(d):
    if not d.candidate_id:
        return None
    c = d.candidate
    return {"slug": c.slug, "full_name": c.full_name, "is_public": c.status == "published" and c.verified_at is not None}


class DonationStatusSerializer(serializers.ModelSerializer):
    """Receipt/status (§7.1): the donor's own view via the reference. Never email, phone, address, declarations."""

    status_label = serializers.CharField(source="get_status_display")
    candidate = serializers.SerializerMethodField()
    net_kobo = serializers.IntegerField(read_only=True)

    class Meta:
        model = Donation
        fields = ["reference", "status", "status_label", "amount_kobo", "refunded_kobo", "net_kobo", "currency", "designation",
                  "candidate", "donor_name", "display_choice", "public_label", "channel", "created_at", "paid_at"]

    def get_candidate(self, d):
        return _candidate(d)


class MyDonationSerializer(DonationStatusSerializer):
    class Meta(DonationStatusSerializer.Meta):
        fields = ["reference", "status", "status_label", "amount_kobo", "refunded_kobo", "net_kobo", "designation", "candidate",
                  "public_label", "display_choice", "created_at", "paid_at"]


class PublicDonationSerializer(serializers.ModelSerializer):
    """/finance (§7.1): label, net amount, designation, day. Nothing else, ever."""

    amount_kobo = serializers.IntegerField(source="net_kobo")
    candidate = serializers.SerializerMethodField()
    paid_on = serializers.SerializerMethodField()

    class Meta:
        model = Donation
        fields = ["public_label", "amount_kobo", "designation", "candidate", "paid_on"]

    def get_candidate(self, d):
        return _candidate(d)

    def get_paid_on(self, d):
        return d.paid_at.date().isoformat() if d.paid_at else None


class AdminDonationSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")
    candidate = serializers.SerializerMethodField()
    net_kobo = serializers.IntegerField(read_only=True)
    donor_user_email = serializers.EmailField(source="donor_user.email", default=None)

    class Meta:
        model = Donation
        fields = ["id", "reference", "provider", "provider_reference", "status", "status_label", "review_reason", "amount_kobo",
                  "refunded_kobo", "net_kobo", "currency", "designation", "candidate", "election_year", "donor_name", "donor_email",
                  "donor_phone", "donor_state", "donor_address", "display_choice", "public_label", "declarations", "channel",
                  "donor_user_email", "created_at", "paid_at", "refunded_at", "last_verified_at"]

    def get_candidate(self, d):
        return _candidate(d)


class AdminDonationEventSerializer(serializers.Serializer):
    source = serializers.CharField()
    event = serializers.CharField()
    status_from = serializers.CharField()
    status_to = serializers.CharField()
    amount_kobo = serializers.IntegerField(allow_null=True)
    currency = serializers.CharField()
    note = serializers.CharField()
    received_at = serializers.DateTimeField()
