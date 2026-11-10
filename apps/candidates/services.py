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


# --------------------------------------------------------------------- geography (spec 19 §5.2)
CONSTITUENCY_KIND = {"senator": "senatorial_district", "house_of_reps": "federal_constituency", "state_assembly": "state_constituency"}


def _geo(c: Candidate, ids: dict):
    """Apply the editor's choices: a constituency for senators, House of Reps and State Assembly; an LGA for LGA chairmen;
    an LGA and ward for councillors. The free-text area is written from the chosen names (kept editable when none)."""
    from apps.geo.models import Constituency
    from apps.geo.services import validate_chain

    if c.position not in CONSTITUENCY_KIND:
        c.geo_constituency = None
    elif "constituency_id" in ids:
        cid = ids["constituency_id"]
        if not cid:
            c.geo_constituency = None
        else:
            con = Constituency.objects.filter(pk=cid).first()
            if con is None or con.kind != CONSTITUENCY_KIND[c.position] or con.state.slug != c.state:
                e = ServiceError("That constituency isn't one this office stands in, in the chosen state.", code="geo_mismatch",
                                 status_code=400)
                e.extra = {"field": "constituency_id"}
                raise e
            if not con.active and c.geo_constituency_id != con.pk:
                e = ServiceError("That constituency is no longer in INEC's current list.", code="geo_inactive", status_code=400)
                e.extra = {"field": "constituency_id"}
                raise e
            c.geo_constituency, c.area_name = con, con.name[:120]
    if c.position in ("lga_chairman", "councillor") and ("lga_id" in ids or "ward_id" in ids):
        lga_id, ward_id = ids.get("lga_id"), ids.get("ward_id") if c.position == "councillor" else None
        if not lga_id and not ward_id:
            c.geo_lga = c.geo_ward = None
        else:
            keep = tuple(f for f, cur, new in (("lga", c.geo_lga_id, lga_id), ("ward", c.geo_ward_id, ward_id)) if cur and cur == new)
            _, lga, ward, _ = validate_chain(c.state, lga_id, ward_id, None, allow_inactive=keep)
            c.geo_lga, c.geo_ward = lga, ward
            c.area_name = lga.name[:120]
            if ward is not None:
                c.ward_name = ward.name[:120]


def _split(data: dict):
    ids = {k: data.pop(k) for k in ("constituency_id", "lga_id", "ward_id") if k in data}
    return data, ids


# --------------------------------------------------------------------- create/edit
@transaction.atomic
def create_candidate(*, data: dict, actor, request=None) -> Candidate:
    data, ids = _split(dict(data))
    c = Candidate(**data, created_by=actor, updated_by=actor, status=S.DRAFT)
    _geo(c, ids)
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

    data, ids = _split(dict(data))
    before = {f: getattr(c, f) for f in ("area_name", "ward_name", "geo_constituency_id", "geo_lga_id", "geo_ward_id")}
    changed = [f for f, v in data.items() if getattr(c, f) != v]
    for f, v in data.items():
        setattr(c, f, v)
    _geo(c, ids)
    changed += [f for f, v in before.items() if getattr(c, f) != v and f not in changed]
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
        _end_activities(c)
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


def _end_activities(c):
    """Spec 18 §18: an unlisted candidate's upcoming events stop taking registrations and volunteering ends."""
    from apps.events.services import end_for_candidate as close_events
    from apps.volunteers.services import end_for_candidate as end_volunteering

    close_events(c)
    end_volunteering(c)


@transaction.atomic
def unpublish(*, candidate, reason, actor, request=None):
    c = _lock(candidate)
    if c.status != S.PUBLISHED:
        raise _conflict("Only published candidates can be unpublished.")
    reason = _require(reason, "reason", "Give a reason for unpublishing.")
    c.status, c.updated_by = S.UNPUBLISHED, actor
    c.save()
    _end_activities(c)
    record(A.CANDIDATE_UNPUBLISHED, target=c, actor=actor, request=request, reason=reason)
    return c


@transaction.atomic
def archive(*, candidate, reason, actor, request=None):
    """'Remove' in FR-035 = archive. Keeps history; revokes all candidate-role links (§6.3)."""
    c = _lock(candidate)
    if c.status == S.ARCHIVED:
        raise _conflict("This candidate is already archived.")
    reason = _require(reason, "reason", "Give a reason for archiving.")
    was_published = c.status == S.PUBLISHED
    c.status, c.updated_by = S.ARCHIVED, actor
    c.save()
    if was_published:
        _end_activities(c)
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
    stored = photos.process_and_store(upload, user=actor)  # raises PhotoError; storage errors surface as 503 in the view
    from apps.media.services import asset_for_name

    with transaction.atomic():
        c = _lock(candidate)
        old = (c.photo_800, c.photo_200)
        c.photo_800, c.photo_200, c.updated_by = stored.name_800, stored.name_200, actor
        c.photo_asset = asset_for_name(stored.name_800)
        c.save()
    photos.delete_stored(*old)
    record(A.CANDIDATE_PHOTO_UPDATED, target=c, actor=actor, request=request, change="uploaded", rights_confirmed=True)
    return c


def remove_photo(*, candidate, actor, request=None):
    with transaction.atomic():
        c = _lock(candidate)
        old = (c.photo_800, c.photo_200)
        c.photo_800, c.photo_200, c.updated_by = "", "", actor
        c.photo_asset = None
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
