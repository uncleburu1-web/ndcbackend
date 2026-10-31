"""
Candidate workflow — spec 03 §6. Every change is audited (§6.4).

    draft ──verify──▶ draft (verified) ──publish──▶ published ⇄ unpublished
    any ──archive──▶ archived ──restore──▶ draft (verification cleared)
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status

from apps.access.models import Role, RoleAssignment, ScopeType
from apps.access.services import RoleError, grant_role, revoke_role
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from . import photos
from .models import IDENTITY_FIELDS, Candidate, CandidateStatus as S

A = AuditLog.Action


def _conflict(message):
    return ServiceError(message, code="invalid_transition", status_code=status.HTTP_409_CONFLICT)


def _require(value: str, field: str, message: str) -> str:
    value = (value or "").strip()
    if not value:
        raise serializers.ValidationError({field: [message]})
    return value


def _full_clean(candidate: Candidate):
    try:
        candidate.full_clean(exclude=["slug"])
    except ValidationError as exc:
        raise serializers.ValidationError(exc.message_dict) from exc


def _lock(candidate: Candidate) -> Candidate:
    return Candidate.objects.select_for_update().get(pk=candidate.pk)


# --------------------------------------------------------------------- create/edit
@transaction.atomic
def create_candidate(*, data: dict, actor, request=None) -> Candidate:
    c = Candidate(**data, created_by=actor, updated_by=actor, status=S.DRAFT)
    _full_clean(c)
    c.assign_slug()
    c.save()
    record(A.CANDIDATE_CREATED, target=c, actor=actor, request=request, slug=c.slug)
    return c


@transaction.atomic
def update_candidate(*, candidate: Candidate, data: dict, actor, request=None) -> tuple[Candidate, bool]:
    """
    Apply edits. Changing identity fields clears verification; on a published record it
    also unpublishes it until re-verified and republished (§6.2).
    Returns (candidate, was_unpublished_by_this_edit).
    """
    c = _lock(candidate)
    if c.status == S.ARCHIVED:
        raise _conflict("Restore this candidate before editing it.")

    changed = [f for f, v in data.items() if getattr(c, f) != v]
    for f, v in data.items():
        setattr(c, f, v)
    _full_clean(c)
    if not changed:
        return c, False

    identity_changed = [f for f in changed if f in IDENTITY_FIELDS]
    unpublished = False
    unverified = bool(identity_changed and c.is_verified)
    if unverified:
        c.verified_at = None
        c.verified_by = None
        if c.status == S.PUBLISHED:
            c.status = S.UNPUBLISHED
            unpublished = True
    c.assign_slug()
    c.updated_by = actor
    c.save()

    record(A.CANDIDATE_UPDATED, target=c, actor=actor, request=request, fields=changed)
    why = "Identity fields changed; needs re-verification."
    if unverified:
        record(A.CANDIDATE_UNVERIFIED, target=c, actor=actor, request=request, reason=why, fields=identity_changed)
    if unpublished:
        record(A.CANDIDATE_UNPUBLISHED, target=c, actor=actor, request=request, reason=why, fields=identity_changed)
    return c, unpublished


# ------------------------------------------------------------------- transitions
@transaction.atomic
def verify(*, candidate, notes, actor, request=None):
    c = _lock(candidate)
    if c.status not in (S.DRAFT, S.UNPUBLISHED):
        raise _conflict("Only draft or unpublished candidates can be verified.")
    notes = _require(notes, "notes", "Write what you checked before verifying.")
    c.verified_at, c.verified_by, c.verification_notes, c.updated_by = timezone.now(), actor, notes, actor
    c.save()
    record(A.CANDIDATE_VERIFIED, target=c, actor=actor, request=request, notes=notes)
    return c


@transaction.atomic
def unverify(*, candidate, actor, request=None):
    c = _lock(candidate)
    if c.status not in (S.DRAFT, S.UNPUBLISHED):
        raise _conflict("Unpublish this candidate before removing verification.")
    if not c.is_verified:
        raise _conflict("This candidate isn't verified.")
    c.verified_at, c.verified_by, c.updated_by = None, None, actor
    c.save()
    record(A.CANDIDATE_UNVERIFIED, target=c, actor=actor, request=request)
    return c


@transaction.atomic
def publish(*, candidate, actor, request=None):
    c = _lock(candidate)
    if c.status not in (S.DRAFT, S.UNPUBLISHED):
        raise _conflict("Only draft or unpublished candidates can be published.")
    if not c.is_verified:
        raise _conflict("Verify this candidate before publishing.")
    _full_clean(c)
    c.status, c.updated_by = S.PUBLISHED, actor
    c.published_at = c.published_at or timezone.now()
    c.save()
    record(A.CANDIDATE_PUBLISHED, target=c, actor=actor, request=request)
    return c


@transaction.atomic
def unpublish(*, candidate, reason, actor, request=None):
    c = _lock(candidate)
    if c.status != S.PUBLISHED:
        raise _conflict("Only published candidates can be unpublished.")
    reason = _require(reason, "reason", "Give a reason for unpublishing.")
    c.status, c.updated_by = S.UNPUBLISHED, actor
    c.save()
    record(A.CANDIDATE_UNPUBLISHED, target=c, actor=actor, request=request, reason=reason)
    return c


@transaction.atomic
def archive(*, candidate, reason, actor, request=None):
    """'Remove' in FR-035 = archive. Keeps history; revokes all candidate-role links (§6.3)."""
    c = _lock(candidate)
    if c.status == S.ARCHIVED:
        raise _conflict("This candidate is already archived.")
    reason = _require(reason, "reason", "Give a reason for archiving.")
    c.status, c.updated_by = S.ARCHIVED, actor
    c.save()
    revoked = 0
    for a in _active_links(c):
        revoke_role(assignment=a, actor=actor, reason=f"Candidate archived: {reason}")
        revoked += 1
    record(A.CANDIDATE_ARCHIVED, target=c, actor=actor, request=request, reason=reason, links_revoked=revoked)
    return c


@transaction.atomic
def restore(*, candidate, actor, request=None):
    c = _lock(candidate)
    if c.status != S.ARCHIVED:
        raise _conflict("Only archived candidates can be restored.")
    c.status, c.verified_at, c.verified_by, c.updated_by = S.DRAFT, None, None, actor
    c.save()  # account links are NOT restored (§6.3)
    record(A.CANDIDATE_RESTORED, target=c, actor=actor, request=request)
    return c


# ------------------------------------------------------------------------ photos
def set_photo(*, candidate, upload, rights_confirmed: bool, actor, request=None):
    if not rights_confirmed:
        raise serializers.ValidationError({"rights_confirmed": ["Confirm NDCConnect may publish this photo."]})
    if candidate.status == S.ARCHIVED:
        raise _conflict("Restore this candidate before changing the photo.")
    stored = photos.process_and_store(upload)  # raises PhotoError; storage errors surface as 503 in the view
    with transaction.atomic():
        c = _lock(candidate)
        old = (c.photo_800, c.photo_200)
        c.photo_800, c.photo_200, c.updated_by = stored.name_800, stored.name_200, actor
        c.save()
    photos.delete_stored(*old)
    record(A.CANDIDATE_PHOTO_UPDATED, target=c, actor=actor, request=request, change="uploaded", rights_confirmed=True)
    return c


def remove_photo(*, candidate, actor, request=None):
    with transaction.atomic():
        c = _lock(candidate)
        old = (c.photo_800, c.photo_200)
        c.photo_800, c.photo_200, c.updated_by = "", "", actor
        c.save()
    photos.delete_stored(*old)
    record(A.CANDIDATE_PHOTO_UPDATED, target=c, actor=actor, request=request, change="removed")
    return c


# ------------------------------------------------------------- account linking
def _active_links(candidate):
    return RoleAssignment.objects.filter(
        role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=candidate.pk, is_active=True
    ).select_related("user")


def linked_accounts(candidate):
    return list(_active_links(candidate))


def link_account(*, candidate, user, actor):
    if candidate.status == S.ARCHIVED:
        raise _conflict("Restore this candidate before linking accounts.")
    try:
        return grant_role(user=user, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, scope_id=candidate.pk,
                          actor=actor, note=f"Linked to candidate {candidate.slug}")
    except RoleError as exc:
        raise ServiceError(str(exc), code="link_failed", status_code=status.HTTP_409_CONFLICT) from exc


def unlink_account(*, candidate, user_id, actor):
    assignment = _active_links(candidate).filter(user_id=user_id).first()
    if not assignment:
        raise ServiceError("That account isn't linked to this candidate.", code="not_found", status_code=status.HTTP_404_NOT_FOUND)
    revoke_role(assignment=assignment, actor=actor, reason=f"Unlinked from candidate {candidate.slug}")
