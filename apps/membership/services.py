"""
Membership rules — spec 06 §6. Every status change happens here, under row locks.

- Payment can start only with a verified PVC, complete s. 77(2) details, and the gate open.
- A fee is paid ONLY when the provider's verification matches amount and NGN (same rule as donations).
- Fees never touch donation records, /finance, or donor limits (MEM- references).
"""
import logging
import re
import uuid
from datetime import date, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.candidates.photos import PhotoError, load_validated_rgb, square_webp
from apps.core import crypto
from apps.core.exceptions import ServiceError
from apps.payments.providers import ProviderError, get_provider
from apps.pvc.services import is_verified as pvc_verified
from apps.pvc.services import status_for as pvc_status_for

from .models import (
    OPEN_PAYMENT_STATES,
    STATE_CODES,
    Membership,
    MembershipEvent,
    MembershipNumberSequence,
    MembershipPayment,
    MembershipPaymentEvent,
    MembershipSettings,
    MembershipStatus as M,
    PaymentStatus as P,
)

logger = logging.getLogger(__name__)

REQUIRED = {
    "full_name": "Enter your full name as on your NIN.",
    "sex": "Choose your sex.",
    "date_of_birth": "Enter your date of birth.",
    "address": "Enter your residential address.",
    "state": "Choose your state of residence.",
    "lga": "Enter your LGA.",
    "ward": "Enter your ward.",
    "polling_unit": "Enter your polling unit.",
}
DECLARATIONS = ("no_other_party", "constitution", "data_processing")
EDITABLE = {M.DRAFT}
REAPPLY_FROM = {M.REJECTED, M.CANCELLED, M.REFUNDED}


# ---------------------------------------------------------------- storage
def private_storage() -> FileSystemStorage:
    """Outside MEDIA_ROOT; never served by /media/ (spec 06 §9)."""
    return FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT, base_url=None)


# ------------------------------------------------------------------- gate
def gate_status() -> dict[str, bool]:
    try:
        provider_ok = get_provider().configured()
    except Exception:
        provider_ok = False
    return {
        "MEMBERSHIP_ENABLED": bool(settings.MEMBERSHIP_ENABLED),
        "payment provider configured": provider_ok,
        "membership fee set": isinstance(settings.MEMBERSHIP_FEE_KOBO, int) and settings.MEMBERSHIP_FEE_KOBO > 0,
        "MEMBERSHIP_DATA_APPROVED": bool(settings.MEMBERSHIP_DATA_APPROVED),
    }


def membership_open() -> bool:
    return all(gate_status().values())


def config_payload() -> dict:
    return {"open": membership_open(), "fee_kobo": settings.MEMBERSHIP_FEE_KOBO,
            "requires_approval": MembershipSettings.current().requires_approval}  # PVC links live at /pvc/config/ (spec 08)


# ---------------------------------------------------------------- helpers
def _event(m, status_from, status_to, actor=None, note=""):
    MembershipEvent.objects.create(membership=m, status_from=status_from, status_to=status_to, actor=actor, note=note[:300])


def age_on(dob: date, today: date | None = None) -> int:
    today = today or timezone.localdate()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def normalise_nin(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def missing_fields(m: Membership) -> dict:
    errors = {f: [msg] for f, msg in REQUIRED.items() if not getattr(m, f)}
    if not m.nin_encrypted:
        errors["nin"] = ["Enter your 11-digit NIN."]
    if not m.photo:
        errors["photo"] = ["Add a passport-style photo."]
    for d in DECLARATIONS:
        if m.declarations.get(d) is not True:
            errors.setdefault("declarations", {})[d] = ["Tick this to continue."]
    if m.date_of_birth and age_on(m.date_of_birth) < 18:
        errors["date_of_birth"] = ["You must be 18 or older to join."]
    return errors


def membership_for(user) -> Membership | None:
    return Membership.objects.filter(user=user).first()


# ---------------------------------------------------------------- details
def save_details(user, data: dict, *, request=None) -> Membership:
    """Drafts can be partial (spec 06 §7.1). Rejected/cancelled/refunded members may start again."""
    with transaction.atomic():
        m, created = Membership.objects.select_for_update().get_or_create(user=user)
        if m.status in REAPPLY_FROM:
            _event(m, m.status, M.DRAFT, user, f"re-applied; previous number {m.membership_number or '—'}")
            m.status, m.membership_number, m.activated_at, m.submitted_at = M.DRAFT, None, None, None
            m.decided_by, m.decision_reason, m.cancelled_at, m.cancelled_by, m.cancel_reason = None, "", None, None, ""
        elif m.status not in EDITABLE:
            raise ServiceError("Your application can't be changed at this stage.", code="invalid_transition", status_code=409)
        for f in ("full_name", "sex", "address", "state", "lga", "ward", "polling_unit"):
            if f in data:
                setattr(m, f, " ".join(str(data[f]).split()))
        if "date_of_birth" in data:
            dob = data["date_of_birth"]
            if dob and age_on(dob) < 18:
                raise ServiceError("You must be 18 or older to join.", code="under_18", status_code=400)
            m.date_of_birth = dob
        if data.get("nin"):
            nin = normalise_nin(data["nin"])
            if not re.fullmatch(r"\d{11}", nin):
                raise ServiceError("Your NIN is 11 digits.", code="validation_error", status_code=400)
            m.nin_encrypted, m.nin_last4 = crypto.encrypt(nin), nin[-4:]
        if "declarations" in data:
            decl = {k: bool(data["declarations"].get(k)) for k in DECLARATIONS}
            m.declarations = {**decl, "declared_at": timezone.now().isoformat()} if all(decl.values()) else decl
        m.save()
        if created:
            _event(m, "", M.DRAFT, user, "application started")
    return m


def set_photo(user, upload, *, consent: bool) -> Membership:
    if not consent:
        raise ServiceError("Confirm this is a recent photo of you that NDC may use on your membership card and register.",
                           code="validation_error", status_code=400)
    try:
        data = square_webp(load_validated_rgb(upload), 600)
    except PhotoError as exc:
        raise ServiceError(str(exc), code="photo_invalid", status_code=400) from None
    storage = private_storage()
    with transaction.atomic():
        m, _ = Membership.objects.select_for_update().get_or_create(user=user)
        if m.status not in EDITABLE | REAPPLY_FROM:
            raise ServiceError("Your application can't be changed at this stage.", code="invalid_transition", status_code=409)
        old = m.photo
        try:
            m.photo = storage.save(f"members/{uuid.uuid4().hex}.webp", ContentFile(data))
        except OSError as exc:
            raise ServiceError("Photo storage is unavailable. Your details are saved; try the photo again later.",
                               code="storage_unavailable", status_code=503) from exc
        m.save(update_fields=["photo", "updated_at"])
    if old:
        try:
            storage.delete(old)
        except OSError:  # pragma: no cover
            pass
    return m


def read_photo(m: Membership) -> bytes | None:
    if not m.photo:
        return None
    with private_storage().open(m.photo, "rb") as fh:
        return fh.read()


# ---------------------------------------------------------------- payment
def start_payment(user, *, request) -> MembershipPayment:
    if not membership_open():
        raise ServiceError("Membership registration opens soon.", code="membership_unavailable", status_code=503)
    if not pvc_verified(user):
        raise ServiceError("Verify your PVC first.", code="pvc_not_verified", status_code=409)
    with transaction.atomic():
        m = Membership.objects.select_for_update().filter(user=user).first()
        if m is None:
            raise ServiceError("Fill in your details first.", code="application_incomplete", status_code=400)
        if MembershipPayment.objects.filter(membership=m, status__in=OPEN_PAYMENT_STATES).exists() or m.status == M.PAYMENT_PENDING:
            raise ServiceError("A payment is already in progress. Finish it or wait a few minutes.", code="payment_in_progress",
                               status_code=409)
        if m.status not in EDITABLE:
            raise ServiceError("Your application isn't waiting for payment.", code="invalid_transition", status_code=409)
        errors = missing_fields(m)
        if errors:
            raise _incomplete(errors)
        provider = get_provider()
        pay = MembershipPayment.objects.create(membership=m, provider=provider.name, amount_kobo=settings.MEMBERSHIP_FEE_KOBO,
                                               payer_email=user.email or "member@ndcconnect.invalid")
        _event(m, m.status, M.PAYMENT_PENDING, user, f"payment {pay.reference}")
        m.status, m.submitted_at = M.PAYMENT_PENDING, timezone.now()
        m.save(update_fields=["status", "submitted_at", "updated_at"])
    record(AuditLog.Action.MEMBERSHIP_SUBMITTED, target=m, actor=user, request=request)
    callback = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/join/complete?reference={pay.reference}"
    try:
        init = get_provider(pay.provider).initialize(pay, callback_url=callback, base_url=request.build_absolute_uri("/"))
    except ProviderError as exc:
        logger.error("Membership payment %s: initialize failed: %s", pay.reference, exc)
        with transaction.atomic():
            p = MembershipPayment.objects.select_for_update().get(pk=pay.pk)
            p.status = P.FAILED
            p.save(update_fields=["status", "updated_at"])
            _pay_event(p, source="system", status_from=P.INITIATED, status_to=P.FAILED, note="initialize failed")
            mm = Membership.objects.select_for_update().get(pk=m.pk)
            _event(mm, mm.status, M.DRAFT, None, "payment could not start")
            mm.status = M.DRAFT
            mm.save(update_fields=["status", "updated_at"])
        raise ServiceError("We couldn't reach the payment provider. No money was taken. Try again.",
                           code="payment_provider_unavailable", status_code=502) from None
    with transaction.atomic():
        p = MembershipPayment.objects.select_for_update().get(pk=pay.pk)
        if init.provider_reference:
            p.provider_reference = init.provider_reference
        if p.status == P.INITIATED:
            p.status = P.PENDING
        p.save()
        _pay_event(p, source="system", event="initialized", status_from=P.INITIATED, status_to=p.status)
    record(AuditLog.Action.MEMBERSHIP_PAYMENT_STARTED, target=m, actor=user, request=request, reference=pay.reference)
    p.checkout_url = init.checkout_url
    return p


def _incomplete(errors: dict):
    from rest_framework.exceptions import ValidationError

    exc = ValidationError(errors)
    exc.envelope_code = "application_incomplete"
    exc.envelope_message = "Some details are missing. Complete the highlighted fields."
    return exc


def _pay_event(p, *, source, event="", status_from="", status_to="", result=None, note="", payload_sha256=""):
    MembershipPaymentEvent.objects.create(
        payment=p, source=source, event=event[:48], status_from=status_from, status_to=status_to,
        amount_kobo=getattr(result, "amount_kobo", None), currency=getattr(result, "currency", "")[:3], note=note[:120],
        payload_sha256=payload_sha256)


def apply_result(payment_id, result, *, source, event="", payload_sha256="", allow_review_clear=False) -> MembershipPayment:
    with transaction.atomic():
        p = MembershipPayment.objects.select_for_update(of=("self",)).select_related("membership").get(pk=payment_id)
        m = Membership.objects.select_for_update().get(pk=p.membership_id)
        prev, prev_refunded, now = p.status, p.refunded_kobo, timezone.now()
        p.last_verified_at = now
        if result.provider_reference and not p.provider_reference:
            p.provider_reference = result.provider_reference[:64]
        if result.channel:
            p.channel = result.channel[:32]
        if result.status == "pending":
            if p.status == P.INITIATED:
                p.status = P.PENDING
        elif result.status in ("failed", "abandoned"):
            if p.status in OPEN_PAYMENT_STATES:
                p.status = P.FAILED if result.status == "failed" else P.ABANDONED
                if m.status == M.PAYMENT_PENDING:
                    _event(m, m.status, M.DRAFT, None, f"payment {p.reference} {p.status}")
                    m.status = M.DRAFT
        elif result.status in ("success", "reversed"):
            if result.amount_kobo != p.amount_kobo or result.currency != "NGN":
                if p.status != P.REFUNDED:
                    p.status, p.review_reason = P.REVIEW, "amount_or_currency_mismatch"
            else:
                if p.status in OPEN_PAYMENT_STATES or (p.status == P.REVIEW and allow_review_clear):
                    p.status, p.review_reason, p.paid_at = P.SUCCESSFUL, "", result.paid_at or p.paid_at or now
                    if m.status == M.PAYMENT_PENDING:
                        if MembershipSettings.current().requires_approval:
                            _event(m, m.status, M.PENDING_APPROVAL, None, f"paid {p.reference}")
                            m.status = M.PENDING_APPROVAL
                        else:
                            _activate(m, actor=None, note=f"paid {p.reference}; approval off")
                refunded = result.refunded_kobo or (p.amount_kobo if result.status == "reversed" else 0)
                refunded = min(max(refunded, p.refunded_kobo), p.amount_kobo)
                if refunded != p.refunded_kobo:
                    p.refunded_kobo, p.refunded_at = refunded, now
                if p.status == P.SUCCESSFUL and p.refunded_kobo >= p.amount_kobo:
                    p.status = P.REFUNDED
                    if m.status == M.REJECTED:
                        _event(m, m.status, M.REFUNDED, None, f"refund {p.reference}")
                        m.status = M.REFUNDED
        p.save()
        m.save()
        if p.status != prev or p.refunded_kobo != prev_refunded:
            _pay_event(p, source=source, event=event, status_from=prev, status_to=p.status, result=result,
                       note=p.review_reason if p.status == P.REVIEW else "", payload_sha256=payload_sha256)
        return p


def verify_now(p, *, source, event="", check_refunds=False, payload_sha256="", allow_review_clear=False):
    result = get_provider(p.provider).verify(p, check_refunds=check_refunds)
    return apply_result(p.pk, result, source=source, event=event, payload_sha256=payload_sha256, allow_review_clear=allow_review_clear)


def maybe_reverify(p, *, min_age_seconds=5):
    if p.status not in OPEN_PAYMENT_STATES:
        return p
    if p.last_verified_at and (timezone.now() - p.last_verified_at).total_seconds() < min_age_seconds:
        return p
    try:
        return verify_now(p, source="verify")
    except ProviderError as exc:
        logger.warning("Membership payment %s: verify failed: %s", p.reference, exc)
        return p


def on_provider_event(provider_name: str, reference: str, event_name: str, payload_sha256: str) -> None:
    """Called by apps.payments.dispatch for MEM- references. Always verifies with the provider."""
    p = MembershipPayment.objects.filter(reference=reference, provider=provider_name).first()
    if p is None:
        logger.warning("Payment webhook %s for unknown membership payment %s", event_name, reference[:40])
        return
    verify_now(p, source="webhook", event=event_name, check_refunds=event_name.startswith("refund."), payload_sha256=payload_sha256)


def sweep(*, older_than_minutes=10, abandon_after_hours=24) -> dict:
    now = timezone.now()
    counts = {"rechecked": 0, "abandoned": 0}
    for p in MembershipPayment.objects.filter(status__in=OPEN_PAYMENT_STATES, created_at__lte=now - timedelta(minutes=older_than_minutes)):
        try:
            p = verify_now(p, source="sweep")
            counts["rechecked"] += 1
        except ProviderError as exc:
            logger.warning("Sweep: verify failed for %s: %s", p.reference, exc)
        if p.status in OPEN_PAYMENT_STATES and p.created_at <= now - timedelta(hours=abandon_after_hours):
            from apps.payments.providers import VerifyResult

            apply_result(p.pk, VerifyResult(status="abandoned", amount_kobo=p.amount_kobo, currency="NGN"), source="sweep",
                         event="no success after 24 h")
            counts["abandoned"] += 1
    return counts


def recheck_payment(m: Membership, *, actor, request=None) -> Membership:
    p = m.payments.order_by("-created_at").first()
    if p is None:
        raise ServiceError("There's no payment to check.", code="invalid_transition", status_code=409)
    try:
        verify_now(p, source="admin_recheck", check_refunds=True, allow_review_clear=True)
    except ProviderError as exc:
        raise ServiceError("The payment provider didn't answer. Try again in a minute.", code="payment_provider_unavailable",
                           status_code=502) from exc
    record(AuditLog.Action.MEMBERSHIP_PAYMENT_RECHECKED, target=m, actor=actor, request=request, reference=p.reference)
    return Membership.objects.get(pk=m.pk)


# ----------------------------------------------------- activation + decisions
def _next_number(state: str) -> str:
    code = STATE_CODES.get(state, "XX")
    year = timezone.localdate().year
    seq, _ = MembershipNumberSequence.objects.select_for_update().get_or_create(state_code=code, year=year)
    seq.last += 1
    seq.save(update_fields=["last"])
    return f"NDC-{code}-{year}-{seq.last:06d}"


def _activate(m: Membership, *, actor, note=""):
    """Caller holds the membership row lock."""
    _event(m, m.status, M.ACTIVE, actor, note)
    m.status, m.activated_at, m.membership_number = M.ACTIVE, timezone.now(), _next_number(m.state)
    record(AuditLog.Action.MEMBERSHIP_ACTIVATED, target=m, actor=actor, number=m.membership_number)


def _has_paid(m) -> bool:
    return m.payments.filter(status=P.SUCCESSFUL).exists()


def approve(m: Membership, *, officer, request=None) -> Membership:
    with transaction.atomic():
        m = Membership.objects.select_for_update().get(pk=m.pk)
        if m.status != M.PENDING_APPROVAL:
            raise ServiceError("Only applications pending approval can be approved.", code="invalid_transition", status_code=409)
        if not _has_paid(m):
            raise ServiceError("The fee hasn't been confirmed by the payment provider.", code="invalid_transition", status_code=409)
        if not pvc_verified(m.user):
            raise ServiceError("This applicant's PVC isn't verified.", code="pvc_not_verified", status_code=409)
        m.decided_by = officer
        _activate(m, actor=officer, note="approved")
        m.save()
    record(AuditLog.Action.MEMBERSHIP_APPROVED, target=m, actor=officer, request=request)
    return m


def reject(m: Membership, *, officer, reason: str, request=None) -> Membership:
    if not (reason or "").strip():
        raise ServiceError("Give a reason the applicant will understand.", code="validation_error", status_code=400)
    with transaction.atomic():
        m = Membership.objects.select_for_update().get(pk=m.pk)
        if m.status != M.PENDING_APPROVAL:
            raise ServiceError("Only applications pending approval can be rejected.", code="invalid_transition", status_code=409)
        _event(m, m.status, M.REJECTED, officer, reason)
        m.status, m.decided_by, m.decision_reason = M.REJECTED, officer, reason.strip()[:300]
        m.save()
    record(AuditLog.Action.MEMBERSHIP_REJECTED, target=m, actor=officer, request=request, reason=reason[:300],
           refund_due=refund_due(m))
    return m


def cancel(m: Membership, *, actor, reason: str, request=None) -> Membership:
    if not (reason or "").strip():
        raise ServiceError("Give a reason for cancelling.", code="validation_error", status_code=400)
    with transaction.atomic():
        m = Membership.objects.select_for_update().get(pk=m.pk)
        if m.status not in (M.ACTIVE, M.PENDING_APPROVAL):
            raise ServiceError("Only active or pending memberships can be cancelled.", code="invalid_transition", status_code=409)
        _event(m, m.status, M.CANCELLED, actor, reason)
        m.status, m.cancelled_at, m.cancelled_by, m.cancel_reason = M.CANCELLED, timezone.now(), actor, reason.strip()[:300]
        m.save()
    record(AuditLog.Action.MEMBERSHIP_CANCELLED, target=m, actor=actor, request=request, reason=reason[:300])
    return m


def refund_due(m: Membership) -> bool:
    return m.status == M.REJECTED and m.payments.filter(status=P.SUCCESSFUL).exists()


def reveal_nin(m: Membership, *, officer, request=None) -> str:
    nin = crypto.decrypt(m.nin_encrypted)
    record(AuditLog.Action.MEMBERSHIP_NIN_REVEALED, target=m, actor=officer, request=request)
    return nin


def set_requires_approval(value: bool, *, admin, request=None) -> MembershipSettings:
    s = MembershipSettings.current()
    if s.requires_approval != value:
        s.requires_approval, s.updated_by = value, admin
        s.save()
        record(AuditLog.Action.MEMBERSHIP_SETTINGS_CHANGED, target=s, actor=admin, request=request, requires_approval=value)
    return s


# ----------------------------------------------------------- user status (§6.4)
def display_status(user) -> str:
    m = membership_for(user)
    pvc = pvc_status_for(user)  # PVC is its own unit; membership only reads its status (spec 08 §6.2)
    if m and m.status in (M.PAYMENT_PENDING, M.PENDING_APPROVAL, M.ACTIVE, M.REJECTED, M.CANCELLED, M.REFUNDED):
        return m.status
    if pvc == "pending":
        return "pvc_pending"
    if pvc == "rejected":
        return "pvc_rejected"
    if pvc == "successful":
        return "pvc_verified"
    if m is None and pvc == "unverified":
        return "not_joined"
    return "pvc_required"


# --------------------------------------------------------------- register (§4.4)
REGISTER_COLUMNS = ["full_name", "sex", "date_of_birth", "address", "state", "lga", "ward", "polling_unit", "nin", "photograph",
                    "membership_number", "member_since"]


def register_queryset(state: str = ""):
    qs = Membership.objects.filter(status=M.ACTIVE).order_by("state", "membership_number")
    return qs.filter(state=state) if state else qs


def register_row(m: Membership) -> list:
    return [m.full_name, m.get_sex_display(), m.date_of_birth.isoformat() if m.date_of_birth else "", m.address,
            m.get_state_display(), m.lga, m.ward, m.polling_unit, crypto.decrypt(m.nin_encrypted),
            f"{m.membership_number}.webp" if m.photo else "", m.membership_number,
            m.activated_at.date().isoformat() if m.activated_at else ""]


# ------------------------------------------------------- account deletion (§6.7)
def erase_for_deleted_user(user) -> bool:
    """PVC data is erased by accounts.mark_deleted itself (spec 08 §6.2)."""
    m = Membership.objects.filter(user=user).first()
    if m is None:
        return False
    with transaction.atomic():
        m = Membership.objects.select_for_update().get(pk=m.pk)
        if m.status not in (M.CANCELLED, M.REJECTED, M.REFUNDED):
            _event(m, m.status, M.CANCELLED, None, "account deleted")
            m.status, m.cancelled_at, m.cancel_reason = M.CANCELLED, timezone.now(), "account deleted"
        photo = m.photo
        m.nin_encrypted, m.nin_last4, m.date_of_birth, m.photo = b"", "", None, ""
        m.save()
    if photo:
        try:
            private_storage().delete(photo)
        except OSError:  # pragma: no cover
            pass
    record(AuditLog.Action.MEMBERSHIP_ERASED, target=m, actor_label="clerk:webhook")
    return True
