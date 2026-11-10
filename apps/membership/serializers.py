from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.candidates.reference import State
from apps.pvc.serializers import GeoPlaceSerializer

from .models import Membership, MembershipPayment, Sex


class DetailsSerializer(serializers.Serializer):
    """Draft details — every field optional here; completeness is checked when paying (spec 06 §7.1)."""

    full_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    sex = serializers.ChoiceField(choices=Sex.choices, required=False, allow_blank=True)
    date_of_birth = serializers.DateField(required=False, allow_null=True, error_messages={"invalid": "Use the date picker (YYYY-MM-DD)."})
    address = serializers.CharField(max_length=300, required=False, allow_blank=True)
    state = serializers.ChoiceField(choices=State.choices, required=False, allow_blank=True)
    lga = serializers.CharField(max_length=120, required=False, allow_blank=True)
    ward = serializers.CharField(max_length=120, required=False, allow_blank=True)
    polling_unit = serializers.CharField(max_length=160, required=False, allow_blank=True)
    # spec 19 §7.2: the picker's choice; the text fields are written from the chosen names
    lga_id = serializers.IntegerField(required=False, allow_null=True)
    ward_id = serializers.IntegerField(required=False, allow_null=True)
    polling_unit_id = serializers.IntegerField(required=False, allow_null=True)
    geo_not_listed = serializers.BooleanField(required=False, help_text="\"My place isn't listed\": keep the text, review it")
    nin = serializers.CharField(max_length=20, required=False, allow_blank=True, write_only=True)
    declarations = serializers.DictField(child=serializers.BooleanField(), required=False)


class PaymentSummarySerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display")

    class Meta:
        model = MembershipPayment
        fields = ["reference", "status", "status_label", "amount_kobo", "refunded_kobo", "channel", "created_at", "paid_at"]


class MyMembershipSerializer(serializers.ModelSerializer):
    """The member's own view — NIN masked, never in full (spec 06 §3.9)."""

    nin_masked = serializers.SerializerMethodField()
    has_photo = serializers.SerializerMethodField()
    state_label = serializers.CharField(source="get_state_display")
    status_label = serializers.CharField(source="get_status_display")
    latest_payment = serializers.SerializerMethodField()
    place = serializers.SerializerMethodField()

    class Meta:
        model = Membership
        fields = ["status", "status_label", "full_name", "sex", "date_of_birth", "address", "state", "state_label", "lga", "ward",
                  "polling_unit", "nin_masked", "has_photo", "declarations", "membership_number", "submitted_at", "activated_at",
                  "decision_reason", "cancel_reason", "latest_payment", "geo_source", "place"]

    @extend_schema_field(GeoPlaceSerializer)
    def get_place(self, m):
        from apps.geo.records import place

        return place(m.geo_lga, m.geo_ward, m.geo_polling_unit)

    def get_nin_masked(self, m):
        return f"•••• {m.nin_last4}" if m.nin_last4 else ""

    def get_has_photo(self, m):
        return bool(m.photo)

    def get_latest_payment(self, m):
        p = m.payments.order_by("-created_at").first()
        return PaymentSummarySerializer(p).data if p else None


class AdminMembershipSerializer(MyMembershipSerializer):
    id = serializers.UUIDField()
    user_email = serializers.EmailField(source="user.email")
    user_phone = serializers.CharField(source="user.phone_number")
    pvc_status = serializers.SerializerMethodField()
    refund_due = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()
    pvc_history = serializers.SerializerMethodField()
    audit = serializers.SerializerMethodField()

    class Meta(MyMembershipSerializer.Meta):
        fields = ["id", "user_email", "user_phone", "pvc_status", "refund_due", "payments", "events", "pvc_history", "audit", "created_at"] + \
                 MyMembershipSerializer.Meta.fields

    def __init__(self, *args, detail=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not detail:  # history is for the detail screen only (spec 06 §4.4); lists stay light
            for f in ("payments", "events", "pvc_history", "audit"):
                self.fields.pop(f)

    def get_events(self, m):
        return [{"status_from": e.status_from, "status_to": e.status_to, "note": e.note, "created_at": e.created_at,
                 "actor_email": e.actor.email if e.actor else None} for e in m.events.select_related("actor")]

    def get_pvc_history(self, m):
        from apps.pvc.models import PvcEvent

        return [{"kind": e.kind, "note": e.note, "created_at": e.created_at, "actor_email": e.actor.email if e.actor else None}
                for e in PvcEvent.objects.filter(user_id=m.user_id).select_related("actor")]

    def get_audit(self, m):
        from apps.audit.models import AuditLog

        rows = AuditLog.objects.filter(target_type="membership.membership", target_id=str(m.pk)).select_related("actor")[:50]
        return [{"action": a.action, "created_at": a.created_at, "actor_email": a.actor.email if a.actor else a.actor_label}
                for a in rows]

    def get_pvc_status(self, m):
        pvc = getattr(m.user, "pvc", None)
        return pvc.status if pvc else "unverified"

    def get_refund_due(self, m):
        from .services import refund_due

        return refund_due(m)

    def get_payments(self, m):
        out = []
        for p in m.payments.order_by("-created_at"):
            row = PaymentSummarySerializer(p).data
            row["provider_reference"] = p.provider_reference
            row["review_reason"] = p.review_reason
            row["events"] = list(p.events.values("source", "event", "status_from", "status_to", "amount_kobo", "currency", "note",
                                                 "received_at"))
            out.append(row)
        return out


class AdminPaymentRowSerializer(PaymentSummarySerializer):
    member_name = serializers.CharField(source="membership.full_name")
    membership_id = serializers.UUIDField()

    class Meta(PaymentSummarySerializer.Meta):
        fields = PaymentSummarySerializer.Meta.fields + ["member_name", "membership_id", "provider_reference", "review_reason"]
