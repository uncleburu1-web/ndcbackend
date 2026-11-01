"""PVC rules — spec 06 §6.1. Officers decide; every decision and VIN reveal is audited."""
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import crypto
from apps.core.exceptions import ServiceError

from .models import PvcEvent, PvcStatus, PvcVerification

VIN_RE = re.compile(r"^[A-Z0-9]{19}$|^[A-Z0-9]{10}$")


def normalise_vin(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw or "").upper()


def valid_vin(vin: str) -> bool:
    return bool(VIN_RE.fullmatch(vin))


def is_verified(user) -> bool:
    return PvcVerification.objects.filter(user=user, status=PvcStatus.SUCCESSFUL).exists()


def _event(user, kind, actor=None, note=""):
    PvcEvent.objects.create(user=user, kind=kind, actor=actor, note=note[:300])


def submit(user, *, vin: str, surname: str, state: str, lga: str, polling_unit: str = "", request=None) -> PvcVerification:
    vin = normalise_vin(vin)
    if not valid_vin(vin):
        raise ServiceError("Enter the VIN exactly as printed on your card (19 letters and numbers), or its last 10 characters.",
                           code="validation_error", status_code=400)
    with transaction.atomic():
        existing = PvcVerification.objects.select_for_update().filter(user=user).first()
        kind = PvcEvent.Kind.DETAILS_CHANGED if existing else PvcEvent.Kind.SUBMITTED
        rec = existing or PvcVerification(user=user)
        rec.vin_encrypted = crypto.encrypt(vin)
        rec.vin_last4 = vin[-4:]
        rec.surname = " ".join(surname.split())[:80]
        rec.state, rec.lga, rec.polling_unit = state, " ".join(lga.split())[:120], polling_unit.strip()[:160]
        rec.status = PvcStatus.PENDING  # new details always need a fresh check (clears a past verification)
        rec.reviewed_by, rec.reviewed_at, rec.reason = None, None, ""
        rec.consent_at = timezone.now()
        rec.save()
        _event(user, kind)
    record(AuditLog.Action.PVC_SUBMITTED, target=rec, actor=user, request=request, changed=bool(existing))
    return rec


def withdraw(user) -> None:
    """Withdrawn submissions are deleted immediately (spec 06 §10). Only a pending check can be withdrawn."""
    with transaction.atomic():
        rec = PvcVerification.objects.select_for_update().filter(user=user).first()
        if rec is None:
            return
        if rec.status != PvcStatus.PENDING:
            raise ServiceError("Only a check that's still pending can be withdrawn.", code="invalid_transition", status_code=409)
        rec.delete()
        _event(user, PvcEvent.Kind.WITHDRAWN)


def _decide(rec_id, officer, to, reason, request):
    with transaction.atomic():
        rec = PvcVerification.objects.select_for_update().get(pk=rec_id)
        if rec.status != PvcStatus.PENDING:
            raise ServiceError("This check has already been decided.", code="invalid_transition", status_code=409)
        rec.status, rec.reviewed_by, rec.reviewed_at, rec.reason = to, officer, timezone.now(), reason[:300]
        rec.save()
        _event(rec.user, PvcEvent.Kind.VERIFIED if to == PvcStatus.SUCCESSFUL else PvcEvent.Kind.REJECTED, officer, reason)
    action = AuditLog.Action.PVC_VERIFIED if to == PvcStatus.SUCCESSFUL else AuditLog.Action.PVC_REJECTED
    record(action, target=rec, actor=officer, request=request, reason=reason[:300])
    return rec


def verify(rec, officer, note="", request=None):
    return _decide(rec.pk, officer, PvcStatus.SUCCESSFUL, note, request)


def reject(rec, officer, reason, request=None):
    if not (reason or "").strip():
        raise ServiceError("Give a reason the voter will understand.", code="validation_error", status_code=400)
    return _decide(rec.pk, officer, PvcStatus.REJECTED, reason.strip(), request)


def reveal_vin(rec, officer, request=None) -> str:
    vin = crypto.decrypt(rec.vin_encrypted)
    _event(rec.user, PvcEvent.Kind.VIN_REVEALED, officer)
    record(AuditLog.Action.PVC_VIN_REVEALED, target=rec, actor=officer, request=request)
    return vin


def prune(*, rejected_after_days=90) -> int:
    """A rejected submission's VIN is deleted after 90 days (spec 06 §10)."""
    cutoff = timezone.now() - timedelta(days=rejected_after_days)
    n = 0
    for rec in PvcVerification.objects.filter(status=PvcStatus.REJECTED, reviewed_at__lte=cutoff).exclude(vin_encrypted=b""):
        rec.vin_encrypted, rec.vin_last4 = b"", ""
        rec.save(update_fields=["vin_encrypted", "vin_last4", "updated_at"])
        _event(rec.user, PvcEvent.Kind.VIN_PRUNED)
        n += 1
    return n


def erase_for_deleted_user(user) -> None:
    """Account deleted: the PVC record goes; events (no VIN) and audit entries stay (spec 06 §6.7)."""
    PvcVerification.objects.filter(user=user).delete()
