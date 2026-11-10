"""NDC membership — spec 06 §5.2. Money is integer kobo; NIN encrypted; photos in private storage."""
import base64
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.fields import StateField, choice_check, state_check

STATE_CODES = {
    "abia": "AB", "adamawa": "AD", "akwa_ibom": "AK", "anambra": "AN", "bauchi": "BA", "bayelsa": "BY", "benue": "BE",
    "borno": "BO", "cross_river": "CR", "delta": "DE", "ebonyi": "EB", "edo": "ED", "ekiti": "EK", "enugu": "EN", "gombe": "GO",
    "imo": "IM", "jigawa": "JI", "kaduna": "KD", "kano": "KN", "katsina": "KT", "kebbi": "KE", "kogi": "KO", "kwara": "KW",
    "lagos": "LA", "nasarawa": "NA", "niger": "NI", "ogun": "OG", "ondo": "ON", "osun": "OS", "oyo": "OY", "plateau": "PL",
    "rivers": "RI", "sokoto": "SO", "taraba": "TA", "yobe": "YO", "zamfara": "ZA", "fct": "FC",
}


class MembershipStatus(models.TextChoices):
    DRAFT = "draft", "Details in progress"
    PAYMENT_PENDING = "payment_pending", "Payment pending"
    PENDING_APPROVAL = "pending_approval", "Pending approval"
    ACTIVE = "active", "Active member"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"
    REFUNDED = "refunded", "Refunded"


class Sex(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"


class GeoSource(models.TextChoices):
    """Spec 19 §11.2."""
    MATCHED = "matched", "Exact match"
    CHOSEN = "chosen", "Chosen by the person"
    REVIEW = "review", "Not listed (needs review)"


class Membership(models.Model):
    """One person's party membership application and register entry (Electoral Act 2026 s. 77(2); spec 06)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="membership")
    status = models.CharField(max_length=20, choices=MembershipStatus.choices, default=MembershipStatus.DRAFT, db_index=True)
    full_name = models.CharField(max_length=150, blank=True)
    sex = models.CharField(max_length=8, choices=Sex.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    address = models.CharField(max_length=300, blank=True)
    state = StateField()
    lga = models.CharField(max_length=120, blank=True)
    ward = models.CharField(max_length=120, blank=True)
    polling_unit = models.CharField(max_length=160, blank=True)
    # spec 16 §8.2: optional links to INEC's geography beside what the person typed (never overwritten)
    geo_lga = models.ForeignKey("geo.Lga", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_ward = models.ForeignKey("geo.Ward", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    geo_polling_unit = models.ForeignKey("geo.PollingUnit", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # spec 19 §7.3: how the links were set ("" none, matched = exact match not yet confirmed, chosen = the person, review = "not listed")
    geo_source = models.CharField(max_length=8, choices=GeoSource.choices, blank=True, default="")
    geo_confirm_asked_at = models.DateTimeField(null=True, blank=True)  # spec 19 §8.1: the geo_confirm notice was sent
    nin_encrypted = models.BinaryField(blank=True, default=b"")
    nin_last4 = models.CharField(max_length=4, blank=True)
    photo = models.CharField(max_length=200, blank=True)  # private storage name
    declarations = models.JSONField(default=dict, blank=True)
    membership_number = models.CharField(max_length=24, unique=True, null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decision_reason = models.CharField(max_length=300, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancel_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(status="active") | (Q(membership_number__isnull=False) & Q(activated_at__isnull=False)),
                name="membership_active_has_number",
            ),
            # spec 16 §6.3
            models.CheckConstraint(condition=~Q(status="cancelled") | Q(cancelled_at__isnull=False), name="membership_membership_cancel_shape"),
            choice_check("status", MembershipStatus.values, "membership_status_valid"),
            state_check("state", "membership_state_valid"),
            choice_check("geo_source", [*GeoSource.values, ""], "membership_geo_source_valid"),
        ]

    def __str__(self):
        return f"membership {self.pk}"


class MembershipNumberSequence(models.Model):
    """Per-state, per-year counter for membership numbers (row-locked when used)."""

    state_code = models.CharField(max_length=2)
    year = models.PositiveIntegerField()
    last = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["state_code", "year"], name="membership_seq_unique")]


class MembershipEvent(models.Model):
    """Append-only history of status changes (incl. a previous application before re-applying)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="events")
    status_from = models.CharField(max_length=20, blank=True)
    status_to = models.CharField(max_length=20, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("MembershipEvent is append-only.")
        super().save(*args, **kwargs)


class PaymentStatus(models.TextChoices):
    INITIATED = "initiated", "Started"
    PENDING = "pending", "Pending"
    SUCCESSFUL = "successful", "Successful"
    FAILED = "failed", "Failed"
    ABANDONED = "abandoned", "Abandoned"
    REVIEW = "review", "Under review"
    REFUNDED = "refunded", "Refunded"


OPEN_PAYMENT_STATES = (PaymentStatus.INITIATED, PaymentStatus.PENDING)


def new_reference() -> str:
    return "MEM-" + base64.b32encode(secrets.token_bytes(13)).decode().rstrip("=")[:20]


class MembershipPayment(models.Model):
    """One membership fee payment; successful only after provider verification (spec 06)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(Membership, on_delete=models.PROTECT, related_name="payments")
    reference = models.CharField(max_length=32, unique=True, default=new_reference, editable=False)
    provider = models.CharField(max_length=16)
    provider_reference = models.CharField(max_length=64, blank=True)
    amount_kobo = models.BigIntegerField()
    refunded_kobo = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3, default="NGN")
    status = models.CharField(max_length=12, choices=PaymentStatus.choices, default=PaymentStatus.INITIATED, db_index=True)
    review_reason = models.CharField(max_length=64, blank=True)
    channel = models.CharField(max_length=32, blank=True)
    payer_email = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_kobo__gt=0), name="membership_payment_amount_positive"),
            models.CheckConstraint(condition=Q(refunded_kobo__gte=0) & Q(refunded_kobo__lte=F("amount_kobo")),
                                   name="membership_payment_refund_within_amount"),
            models.CheckConstraint(condition=Q(currency="NGN"), name="membership_payment_ngn_only"),
            models.UniqueConstraint(fields=["membership"], condition=Q(status__in=["initiated", "pending"]),
                                    name="membership_one_open_payment"),
            # spec 16 §6.3
            models.CheckConstraint(condition=~Q(status="successful") | Q(paid_at__isnull=False), name="membership_payment_paid_shape"),
            models.CheckConstraint(condition=~Q(status="refunded") | (Q(refunded_at__isnull=False) & Q(refunded_kobo=F("amount_kobo"))),
                                   name="membership_payment_refund_shape"),
            choice_check("status", PaymentStatus.values, "membership_payment_status_valid"),
        ]

    def __str__(self):
        return self.reference

    @property
    def metadata(self) -> dict:  # payable interface (spec 06 §6.3)
        return {"kind": "membership_fee", "membership_id": str(self.membership_id)}


class MembershipPaymentEvent(models.Model):
    """Append-only history of a membership payment (webhooks, verifications, status changes)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(MembershipPayment, on_delete=models.PROTECT, related_name="events")
    source = models.CharField(max_length=16)
    event = models.CharField(max_length=48, blank=True)
    status_from = models.CharField(max_length=12, blank=True)
    status_to = models.CharField(max_length=12, blank=True)
    amount_kobo = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    note = models.CharField(max_length=120, blank=True)
    payload_sha256 = models.CharField(max_length=64, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["received_at"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError("MembershipPaymentEvent is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("MembershipPaymentEvent is append-only.")


class MembershipSettings(models.Model):
    """Single row. Only system admins change it (spec 06 §6.5)."""

    requires_approval = models.BooleanField(default=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name="membership_settings_single_row")]  # spec 16 §6.3

    @classmethod
    def current(cls) -> "MembershipSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
