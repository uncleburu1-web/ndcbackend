"""
PVC rules — spec 06 §6.1, made a stand-alone unit in spec 08. Officers check INEC's public voter checker by hand
and record the result; every decision and VIN reveal is audited. Other apps call only status_for / is_verified.
"""
import re
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import crypto
from apps.core.exceptions import ServiceError

from .models import GeoSource, PvcEvent, PvcStatus, PvcVerification

VIN_RE = re.compile(r"^[A-Z0-9]{19}$|^[A-Z0-9]{10}$")


def normalise_vin(raw: str) -> str:
    return re.sub(r"[\s-]", "", raw or "").upper()


def valid_vin(vin: str) -> bool:
    return bool(VIN_RE.fullmatch(vin))


UNVERIFIED = "unverified"


def status_for(user) -> str:
    """unverified · pending · successful · rejected — the one function other apps call (spec 08 §6.1)."""
    if user is None or not getattr(user, "pk", None):
        return UNVERIFIED
    st = PvcVerification.objects.filter(user=user).values_list("status", flat=True).first()
    return st or UNVERIFIED


def is_verified(user) -> bool:
    return status_for(user) == PvcStatus.SUCCESSFUL


def public_badge(user) -> bool:
    """Public "Verified voter" chip: verified AND the user opted in (spec 08 §3.7)."""
    if user is None:
        return False
    return PvcVerification.objects.filter(user=user, status=PvcStatus.SUCCESSFUL, show_badge=True).exists()


def community_badge(user) -> bool:
    """The badge on community posts and comments: as `public_badge`, plus version 2 of the badge consent (spec 17 §17.1)."""
    if not public_badge(user):
        return False
    from apps.privacy.consent import active

    rec = active(user, "verified_badge")
    return bool(rec and rec.version >= 2)


def verified_location(user):
    """Spec 17 §5.2, keeping 08's boundary: (state, geo_lga_id, geo_ward_id, geo_polling_unit_id) for a successful check, else None.
    The ward comes from the polling unit when the record has no ward link of its own. Never returns PVC details."""
    if user is None or not getattr(user, "pk", None):
        return None
    rec = (PvcVerification.objects.filter(user=user, status=PvcStatus.SUCCESSFUL)
           .select_related("geo_polling_unit__ward__lga").only("state", "geo_source", "geo_lga_id", "geo_ward_id", "geo_polling_unit").first())
    if rec is None or not rec.state:
        return None
    if rec.geo_source in (GeoSource.CHOSEN, GeoSource.REVIEW):
        # spec 19 §8.1: only officer-confirmed or exact-match links place people below state level
        return rec.state, None, None, None
    pu = rec.geo_polling_unit
    ward_id = rec.geo_ward_id or (pu.ward_id if pu else None)
    lga_id = rec.geo_lga_id or (pu.ward.lga_id if pu else None)
    return rec.state, lga_id, ward_id, pu.pk if pu else None


def _placement_changed(user):
    """Tell communities a PVC result changed (spec 17 §5.2): an outbox job with the user ID only."""
    from apps.core import outbox

    outbox.enqueue("communities_place", {"user": str(user.pk)}, dedupe_key=f"communities_place:{user.pk}")


def _event(user, kind, actor=None, note=""):
    PvcEvent.objects.create(user=user, kind=kind, actor=actor, note=note[:300])


def submit(user, *, vin: str, surname: str, state: str, lga: str = "", polling_unit: str = "", geo=None, request=None) -> PvcVerification:
    """`geo` is the picker's choice (spec 19 §7.2: state and LGA required; ward and unit optional), or None for typed text."""
    from apps.privacy.consent import require_consent

    require_consent(user, "pvc_check")  # spec 14 §4.2
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
        rec.geo_lga, rec.geo_ward, rec.geo_polling_unit = None, None, None  # new details: match_geography links them again (spec 17)
        rec.pending_geo_lga = rec.pending_geo_ward = rec.pending_geo_polling_unit = None
        rec.geo_source, rec.geo_confirm_asked_at = "", None
        if geo is not None:
            from apps.geo.records import set_choice

            set_choice(rec, "pvc", state, geo, fields=("lga", "polling_unit"))  # spec 19 §7.3 (chain checked; text from names)
        if not rec.lga:
            raise ServiceError("Enter the LGA you registered in.", code="validation_error", status_code=400)
        rec.status = PvcStatus.PENDING  # new details always need a fresh check (clears a past verification)
        rec.reviewed_by, rec.reviewed_at, rec.reason = None, None, ""
        rec.show_badge = False  # a new check switches the public badge off (spec 08 §5.1)
        rec.consent_at = timezone.now()
        rec.save()
        _event(user, kind)
        if existing:
            _placement_changed(user)  # a past verification is cleared: verified community memberships end (spec 17 §5.2)
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


def _decide(rec_id, officer, to, reason, request, geo=None):
    from apps.notifications.services import notify_pvc_decision

    with transaction.atomic():
        rec = PvcVerification.objects.select_for_update(of=("self",)).select_related("user").get(pk=rec_id)
        if rec.user_id == officer.pk:
            raise ServiceError("You can't decide your own PVC check.", code="self_review", status_code=403)
        if rec.status != PvcStatus.PENDING:
            raise ServiceError("This check has already been decided.", code="invalid_transition", status_code=409)
        rec.status, rec.reviewed_by, rec.reviewed_at, rec.reason = to, officer, timezone.now(), reason[:300]
        if geo is not None and to == PvcStatus.SUCCESSFUL and not geo.get("polling_unit"):
            raise ServiceError("Choose the polling unit INEC's checker showed.", code="validation_error", status_code=400)
        if geo is not None and to == PvcStatus.SUCCESSFUL:
            # spec 19 §8.2: the officer confirms or corrects the polling unit from INEC's checker; communities use this link
            from apps.geo.records import set_choice

            set_choice(rec, "pvc", rec.state, {**geo, "not_listed": False}, source=GeoSource.OFFICER, fields=("lga", "polling_unit"))
            rec.pending_geo_lga = rec.pending_geo_ward = rec.pending_geo_polling_unit = None
        rec.save()
        _event(rec.user, PvcEvent.Kind.VERIFIED if to == PvcStatus.SUCCESSFUL else PvcEvent.Kind.REJECTED, officer, reason)
        # Same transaction: the decision and the user's notification commit together (spec 08 §11).
        notify_pvc_decision(rec.user, "verified" if to == PvcStatus.SUCCESSFUL else "rejected")
        _placement_changed(rec.user)  # spec 17 §5.2
    action = AuditLog.Action.PVC_VERIFIED if to == PvcStatus.SUCCESSFUL else AuditLog.Action.PVC_REJECTED
    record(action, target=rec, actor=officer, request=request, reason=reason[:300],
           **({"geo_confirmed": True} if geo is not None and to == PvcStatus.SUCCESSFUL else {}))
    return rec


def verify(rec, officer, note="", request=None, geo=None):
    return _decide(rec.pk, officer, PvcStatus.SUCCESSFUL, note, request, geo=geo)


def reject(rec, officer, reason, request=None):
    if not (reason or "").strip():
        raise ServiceError("Give a reason the voter will understand.", code="validation_error", status_code=400)
    return _decide(rec.pk, officer, PvcStatus.REJECTED, reason.strip(), request)


def set_badge(user, show: bool) -> PvcVerification:
    if show:
        from apps.privacy.consent import require_consent

        require_consent(user, "verified_badge")  # spec 14 §4.2
    with transaction.atomic():
        rec = PvcVerification.objects.select_for_update().filter(user=user).first()
        if rec is None or rec.status != PvcStatus.SUCCESSFUL:
            raise ServiceError("The badge is available once your PVC is verified.", code="invalid_transition", status_code=409)
        if rec.show_badge != show:
            rec.show_badge = show
            rec.save(update_fields=["show_badge", "updated_at"])
            _event(user, PvcEvent.Kind.BADGE_CHANGED, user, "shown publicly" if show else "hidden")
    return rec


def update_settings(admin, data: dict, request=None):
    from .models import PvcSettings

    s = PvcSettings.current()
    changed = []
    for field, limit in (("guidance_note", 1000), ("collection_note", 500)):
        if field in data:
            value = str(data[field] or "").strip()
            if len(value) > limit:
                raise ServiceError(f"Keep it under {limit} characters.", code="validation_error", status_code=400)
            if getattr(s, field) != value:
                setattr(s, field, value)
                changed.append(field)
    if changed:
        s.updated_by = admin
        s.save()
        record(AuditLog.Action.PVC_SETTINGS_CHANGED, target=s, actor=admin, request=request, fields=changed)
    return s


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


def erase_for_withdrawn_consent(user) -> bool:
    """Spec 14 §4.2: withdrawing PVC consent deletes the record (VIN included)."""
    n, _ = PvcVerification.objects.filter(user=user).delete()
    if n:
        _event(user, PvcEvent.Kind.WITHDRAWN, user, "consent withdrawn")
        from apps.communities.services import end_verified

        end_verified(user)  # same transaction: verified community memberships end at once (spec 17 §5.2)
    return bool(n)


def erase_for_deleted_user(user) -> None:
    """Account deleted: the PVC record goes; events (no VIN) and audit entries stay (spec 06 §6.7)."""
    PvcVerification.objects.filter(user=user).delete()
