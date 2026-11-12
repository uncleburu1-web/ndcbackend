"""
Consent for political-opinion processing (spec 14 §4.2, §6.1). Each purpose has a version; a record of the
current (or a non-material older) version must exist before the owning app processes the data. Withdrawal
undoes the processing through the owning app's own service, in one transaction.
"""
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from .models import ConsentRecord, Purpose
from .register import CONSENT_PURPOSES

LEGACY_PROMPTS = 3


def _purpose(purpose):
    p = CONSENT_PURPOSES.get(purpose)
    if p is None:
        raise ServiceError("Unknown purpose.", code="not_found", status_code=404)
    return p


def active(user, purpose):
    return ConsentRecord.objects.filter(user=user, purpose=purpose, withdrawn_at__isnull=True).first()


def has_consent(user, purpose) -> bool:
    rec = active(user, purpose)
    if rec is None:
        return False
    return rec.version >= _purpose(purpose).material_since


def require_consent(user, purpose):
    if not getattr(settings, "PRIVACY_CONSENT_REQUIRED", True):
        return
    if not has_consent(user, purpose):
        p = _purpose(purpose)
        e = ServiceError(f"Agree to how NDCConnect uses this information first: {p.label.lower()}.", code="consent_required",
                         status_code=403)
        e.extra = {"purpose": p.key, "version": p.version, "text": p.text, "label": p.label}
        raise e


def grant(user, purpose, method=ConsentRecord.Method.DIALOG) -> ConsentRecord:
    p = _purpose(purpose)
    with transaction.atomic():
        rec = ConsentRecord.objects.select_for_update().filter(user=user, purpose=purpose, withdrawn_at__isnull=True).first()
        if rec and rec.version >= p.version:
            if rec.method == ConsentRecord.Method.BEFORE_RECORDS and not rec.confirmed_at:
                rec.confirmed_at = timezone.now()
                rec.save(update_fields=["confirmed_at"])
            return rec
        if rec:  # an older version: close it and record the new one
            rec.withdrawn_at, rec.withdrawal_effect = timezone.now(), f"replaced by version {p.version}"
            rec.save(update_fields=["withdrawn_at", "withdrawal_effect"])
        try:
            with transaction.atomic():
                new = ConsentRecord.objects.create(user=user, purpose=purpose, version=p.version, method=method)
        except IntegrityError:  # a parallel grant
            return active(user, purpose)
        if purpose == Purpose.COMMUNITY_MEMBERSHIP:  # spec 17 §5.2: placement runs once consent is given
            from apps.core import outbox

            outbox.enqueue("communities_place", {"user": str(user.pk)}, dedupe_key=f"communities_place:{user.pk}")
        return new


def _undo(user, purpose, options=None) -> str:
    options = options or {}
    if purpose == "community_membership":
        from apps.communities.services import withdraw as leave_all

        return leave_all(user, delete_content=bool(options.get("delete_content")))
    if purpose == "event_registration":  # spec 18 §17.1
        from apps.events.services import cancel_upcoming_for

        n = cancel_upcoming_for(user)
        return f"{n} upcoming registration{'s' if n != 1 else ''} cancelled"
    if purpose == "volunteering":
        from apps.volunteers.services import withdraw_all

        n = withdraw_all(user)
        return f"{n} volunteer application{'s' if n != 1 else ''} withdrawn"
    if purpose == "political_following":
        from apps.follows.services import remove_all_for_user

        n = remove_all_for_user(user)
        return f"{n} follow{'s' if n != 1 else ''} removed"
    if purpose == "membership":
        from apps.membership.services import erase_for_withdrawn_consent

        return "membership cancelled and register details erased" if erase_for_withdrawn_consent(user) else "no membership on file"
    if purpose == "pvc_check":
        from apps.pvc.services import erase_for_withdrawn_consent as erase_pvc

        return "PVC details deleted" if erase_pvc(user) else "no PVC details on file"
    if purpose == "public_profile":  # spec 20 §5.1
        from apps.accounts.settings_services import profile_off

        return "public profile turned off" if profile_off(user) else "public profile was already off"
    if purpose == "verified_badge":
        from apps.pvc.models import PvcVerification

        n = PvcVerification.objects.filter(user=user, show_badge=True).update(show_badge=False)
        return "badge turned off" if n else "badge was already off"
    return ""


def withdraw(user, purpose, request=None, **options) -> dict:
    """`options`: `delete_content` for communities (spec 17 §5.4)."""
    _purpose(purpose)
    with transaction.atomic():
        rec = ConsentRecord.objects.select_for_update().filter(user=user, purpose=purpose, withdrawn_at__isnull=True).first()
        effect = _undo(user, purpose, options)  # raises (and rolls back) if the owning app refuses
        if rec:
            rec.withdrawn_at, rec.withdrawal_effect = timezone.now(), effect[:200]
            rec.save(update_fields=["withdrawn_at", "withdrawal_effect"])
    record(AuditLog.Action.PRIVACY_CONSENT_WITHDRAWN, target=rec, actor=user, request=request, purpose=purpose)
    return {"purpose": purpose, "effect": effect}


def preview_withdrawal(user, purpose) -> str:
    """What withdrawing would do, for the confirmation dialog."""
    if purpose == "political_following":
        from apps.follows.models import CandidateFollow

        n = CandidateFollow.objects.filter(user=user).count()
        return f"This removes your {n} follow{'s' if n != 1 else ''} and their notifications."
    return _purpose(purpose).withdraw_effect


def summary(user) -> list[dict]:
    out = []
    for p in CONSENT_PURPOSES.values():
        rec = active(user, p.key)
        out.append({
            "purpose": p.key, "label": p.label, "current_version": p.version, "text": p.text,
            "given": bool(rec), "version": rec.version if rec else None, "granted_at": rec.granted_at if rec else None,
            "method": rec.method if rec else None, "needs_confirmation": bool(rec and p.key == Purpose.POLITICAL_FOLLOWING and rec.method == ConsentRecord.Method.BEFORE_RECORDS
                                                                               and not rec.confirmed_at and rec.prompts_shown < LEGACY_PROMPTS),
            "outdated": bool(rec and rec.version < p.material_since), "withdraw_effect": preview_withdrawal(user, p.key) if rec else "",
        })
    return out


def consents_needed(user) -> list[str]:
    """Purposes needing a fresh answer: a material version change, or a legacy follows record to confirm (spec 14 §4.2)."""
    out = []
    for p in CONSENT_PURPOSES.values():
        rec = active(user, p.key)
        if rec is None:
            continue
        if rec.version < p.material_since:
            out.append(p.key)
        elif p.key == Purpose.VERIFIED_BADGE and rec.version < p.version and rec.prompts_shown < LEGACY_PROMPTS:
            from apps.pvc.services import public_badge

            if public_badge(user):
                out.append(p.key)  # spec 17 §17.1: asked again before the badge shows in communities
        elif (p.key == Purpose.POLITICAL_FOLLOWING and rec.method == ConsentRecord.Method.BEFORE_RECORDS and not rec.confirmed_at
              and rec.prompts_shown < LEGACY_PROMPTS):
            out.append(p.key)  # only follows get the legacy confirmation prompt (spec 14 §4.2)
    return out


def mark_prompt_shown(user, purpose):
    ConsentRecord.objects.filter(user=user, purpose=purpose, withdrawn_at__isnull=True, method=ConsentRecord.Method.BEFORE_RECORDS,
                                 confirmed_at__isnull=True).update(prompts_shown=F("prompts_shown") + 1)
