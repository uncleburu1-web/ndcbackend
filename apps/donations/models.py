"""Donations — spec 05 §5. Money is integer kobo, NGN only."""
import base64
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from apps.core.fields import StateField, choice_check, state_check


class DonationStatus(models.TextChoices):
    INITIATED = "initiated", "Initiated"
    PENDING = "pending", "Pending"
    SUCCESSFUL = "successful", "Successful"
    FAILED = "failed", "Failed"
    ABANDONED = "abandoned", "Abandoned"
    REFUNDED = "refunded", "Refunded"
    REVIEW = "review", "Under review"


class Designation(models.TextChoices):
    PARTY = "party", "NDC general fund"
    CANDIDATE = "candidate", "Candidate"


class DisplayChoice(models.TextChoices):
    NAME = "name", "Show my name"
    ANONYMOUS = "anonymous", "Anonymous Donor"
    GUEST = "guest", "Guest"


def new_reference() -> str:
    """NDC- + 20 base32 chars (~100 bits): unguessable, so it can key a receipt."""
    return "NDC-" + base64.b32encode(secrets.token_bytes(13)).decode().rstrip("=")[:20]


class Donation(models.Model):
    """One donation attempt, its verified status and the donor details the law requires (spec 05). Money in kobo, NGN only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=32, unique=True, default=new_reference, editable=False)
    provider = models.CharField(max_length=16)
    provider_reference = models.CharField(max_length=64, blank=True)
    amount_kobo = models.BigIntegerField()
    refunded_kobo = models.BigIntegerField(default=0)
    currency = models.CharField(max_length=3, default="NGN")
    designation = models.CharField(max_length=12, choices=Designation.choices)
    candidate = models.ForeignKey("candidates.Candidate", null=True, blank=True, on_delete=models.PROTECT, related_name="donations")
    election_year = models.PositiveIntegerField(null=True, blank=True)
    donor_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="donations")
    donor_name = models.CharField(max_length=150)
    donor_email = models.EmailField()
    donor_phone = models.CharField(max_length=14)
    donor_state = StateField(blank=False)
    donor_address = models.CharField(max_length=300)
    display_choice = models.CharField(max_length=12, choices=DisplayChoice.choices)
    public_label = models.CharField(max_length=150, blank=True)  # snapshot at success
    declarations = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=DonationStatus.choices, default=DonationStatus.INITIATED)
    review_reason = models.CharField(max_length=64, blank=True)
    channel = models.CharField(max_length=32, blank=True)
    initiated_ip_hash = models.CharField(max_length=64, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(condition=Q(amount_kobo__gt=0), name="donations_amount_positive"),
            models.CheckConstraint(condition=Q(refunded_kobo__gte=0) & Q(refunded_kobo__lte=F("amount_kobo")),
                                   name="donations_refund_within_amount"),
            models.CheckConstraint(condition=Q(currency="NGN"), name="donations_ngn_only"),
            models.CheckConstraint(
                condition=(Q(designation="candidate", candidate__isnull=False) | Q(designation="party", candidate__isnull=True)),
                name="donations_designation_consistent",
            ),
            # spec 16 §6.3: status ⇄ timestamp shapes, valid values, plausible years
            models.CheckConstraint(condition=~Q(status="successful") | Q(paid_at__isnull=False), name="donations_donation_paid_shape"),
            models.CheckConstraint(condition=~Q(status="refunded") | (Q(refunded_at__isnull=False) & Q(refunded_kobo=F("amount_kobo"))),
                                   name="donations_donation_refund_shape"),
            choice_check("status", DonationStatus.values, "donations_status_valid"),
            state_check("donor_state", "donations_donor_state_valid", allow_blank=False),
            models.CheckConstraint(condition=Q(election_year__isnull=True) | Q(election_year__gte=2023, election_year__lte=2099), name="donations_election_year_plausible"),
        ]
        indexes = [
            models.Index(fields=["status", "-paid_at"], name="donations_public_idx"),
            models.Index(fields=["candidate", "status"], name="donations_candidate_idx"),
            models.Index(fields=["donor_user", "-created_at"], name="donations_user_idx"),
            models.Index(fields=["donor_email"], name="donations_email_idx"),
            models.Index(fields=["donor_phone"], name="donations_phone_idx"),
        ]

    def __str__(self):
        return self.reference

    @property
    def net_kobo(self) -> int:
        return self.amount_kobo - self.refunded_kobo

    # payable interface for the shared payment providers (spec 06 §6.3)
    @property
    def payer_email(self) -> str:
        return self.donor_email

    @property
    def metadata(self) -> dict:
        return {"kind": "donation", "donation_id": str(self.id)}


class DonationEvent(models.Model):
    """Append-only history of status changes (spec 05 §5.2). Never the full provider payload."""

    class Source(models.TextChoices):
        WEBHOOK = "webhook"
        VERIFY = "verify"
        SWEEP = "sweep"
        ADMIN_RECHECK = "admin_recheck"
        SYSTEM = "system"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    donation = models.ForeignKey(Donation, on_delete=models.PROTECT, related_name="events")
    source = models.CharField(max_length=16, choices=Source.choices)
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
            raise RuntimeError("DonationEvent is append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("DonationEvent is append-only.")
