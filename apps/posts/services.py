"""
Candidate posts — spec 09 §6.1. Linked campaign accounts publish directly (no Pending). Every rule is checked
here, not only in views: role scope, published candidate, office-scoped blackout, self-moderation.
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.access.models import Role, ScopeType
from apps.access.services import has_role, is_system_admin
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import blackouts
from apps.core.exceptions import ServiceError
from apps.core.i18n import clean_content_language
from apps.news import images
from apps.news.services import clean_text

from .models import CandidatePost, PostEvent, PostKind
from .models import PostStatus as S

A = AuditLog.Action


def can_post(user, candidate) -> bool:
    return bool(user and user.is_authenticated and has_role(user, Role.CANDIDATE, ScopeType.CANDIDATE, candidate.pk))


def linked_candidates(user):
    from apps.access.models import RoleAssignment
    from apps.candidates.models import Candidate

    ids = RoleAssignment.objects.filter(user=user, role=Role.CANDIDATE, scope_type=ScopeType.CANDIDATE, is_active=True)\
        .values_list("scope_id", flat=True)
    return Candidate.objects.filter(pk__in=list(ids)).order_by("full_name")


def candidate_published(candidate) -> bool:
    return candidate.status == "published" and candidate.verified_at is not None


def paused_until(candidate):
    w = blackouts.active_window(positions=[candidate.position])
    return w["end"] if w else None


def _require_can_post(user, candidate):
    if not can_post(user, candidate):
        raise ServiceError("You're not linked to this candidate.", code="permission_denied", status_code=403)


def _require_published(candidate):
    if not candidate_published(candidate):
        raise ServiceError("This candidate isn't published, so posts can't go out right now.", code="candidate_not_published",
                           status_code=409)


def _require_not_paused(candidate):
    until = paused_until(candidate)
    if until:
        exc = ServiceError(f"Campaigning ends 24 hours before polls. Posting is paused until {timezone.localtime(until):%d %b %Y, %H:%M}.",
                           code="posting_paused", status_code=409)
        exc.extra = {"until": until.isoformat()}
        raise exc


def _event(post, kind, actor=None, note=""):
    PostEvent.objects.create(post=post, event=kind, actor=actor, note=(note or "")[:300])


def _locked(post) -> CandidatePost:
    return CandidatePost.objects.select_for_update(of=("self",)).select_related("candidate").get(pk=post.pk)


def _validate(data: dict, partial=False) -> dict:
    out, errors = {}, {}
    if not partial or "kind" in data:
        if data.get("kind") not in PostKind.values:
            errors["kind"] = ["Choose Announcement or News."]
        else:
            out["kind"] = data["kind"]
    if not partial or "title" in data:
        title = (data.get("title") or "").strip()
        if not title:
            errors["title"] = ["Give it a title."]
        elif len(title) > 150:
            errors["title"] = ["Keep the title under 150 characters."]
        out["title"] = title
    if not partial or "body" in data:
        body = clean_text(data.get("body") or "")
        if not body:
            errors["body"] = ["Write the post."]
        elif len(body) > settings.CANDIDATE_POST_MAX_BODY:
            errors["body"] = [f"Keep it under {settings.CANDIDATE_POST_MAX_BODY:,} characters."]
        out["body"] = body
    if "language" in data and data["language"] not in (None, ""):
        out["language"] = clean_content_language(data["language"], "und")
    if errors:
        raise serializers.ValidationError(errors)
    return out


# ------------------------------------------------------------------ campaign
def create_post(candidate, author, data: dict, *, request=None) -> CandidatePost:
    _require_can_post(author, candidate)
    _require_published(candidate)
    _require_not_paused(candidate)
    clean = _validate(data)
    with transaction.atomic():
        post = CandidatePost.objects.create(candidate=candidate, published_at=timezone.now(), created_by=author, updated_by=author, **clean)
        _event(post, PostEvent.Kind.PUBLISHED, author)
        from apps.notifications.services import deliver_post_on_commit

        deliver_post_on_commit(post)
    record(A.POST_CREATED, target=post, actor=author, request=request, candidate=str(candidate.pk), kind=post.kind)
    return post


def update_post(post, author, data: dict, *, request=None) -> CandidatePost:
    _require_can_post(author, post.candidate)
    _require_published(post.candidate)
    _require_not_paused(post.candidate)
    clean = _validate(data, partial=True)
    with transaction.atomic():
        p = _locked(post)
        if p.status != S.PUBLISHED:
            raise ServiceError("Only published posts can be edited.", code="invalid_transition", status_code=409)
        changed = [k for k, v in clean.items() if getattr(p, k) != v]
        if changed:
            for k in changed:
                setattr(p, k, clean[k])
            p.edited_at, p.updated_by = timezone.now(), author
            p.save()
            _event(p, PostEvent.Kind.EDITED, author, ", ".join(changed))
    if changed:
        record(A.POST_UPDATED, target=p, actor=author, request=request, fields=changed)  # no delivery on edit (spec 09 §3.6)
    return p


FIRST_IMAGE_GRACE = timedelta(minutes=5)


def set_image(post, author, upload, *, rights_confirmed: bool, request=None) -> CandidatePost:
    _require_can_post(author, post.candidate)
    _require_published(post.candidate)
    _require_not_paused(post.candidate)
    if not rights_confirmed:
        raise serializers.ValidationError({"rights_confirmed": ["Confirm you took this photo or have the right to share it."]})
    stored = images.store(upload, purpose="post_image", user=author)
    from apps.media.services import asset_for_name

    with transaction.atomic():
        p = _locked(post)
        if p.status != S.PUBLISHED:
            images.delete(stored["image"], stored["image_thumb"])
            raise ServiceError("Only published posts can be changed.", code="invalid_transition", status_code=409)
        old = (p.image, p.image_thumb)
        for k, v in stored.items():
            setattr(p, k, v)
        p.image_asset = asset_for_name(stored["image"])
        now = timezone.now()
        # The publish form sends the image straight after creating the post; that first image is part of publishing, not an edit.
        if old[0] or now - p.published_at > FIRST_IMAGE_GRACE:
            p.edited_at = now
        p.updated_by = author
        p.save()
        _event(p, PostEvent.Kind.IMAGE_CHANGED, author, "uploaded")
    images.delete(*old)
    record(A.POST_IMAGE_UPDATED, target=p, actor=author, request=request, change="uploaded", rights_confirmed=True)
    return p


def remove_image(post, author, *, request=None) -> CandidatePost:
    _require_can_post(author, post.candidate)
    _require_not_paused(post.candidate)
    with transaction.atomic():
        p = _locked(post)
        if p.status != S.PUBLISHED:
            raise ServiceError("Only published posts can be changed.", code="invalid_transition", status_code=409)
        old = (p.image, p.image_thumb)
        if any(old):
            p.image = p.image_thumb = ""
            p.image_width = p.image_height = None
            p.image_asset = None
            p.edited_at, p.updated_by = timezone.now(), author
            p.save()
            _event(p, PostEvent.Kind.IMAGE_CHANGED, author, "removed")
    images.delete(*old)
    if any(old):
        record(A.POST_IMAGE_UPDATED, target=p, actor=author, request=request, change="removed")
    return p


def delete_post(post, author, *, request=None) -> CandidatePost:
    """Allowed during blackouts — a campaign can always take something down (spec 09 §6.6)."""
    _require_can_post(author, post.candidate)
    with transaction.atomic():
        p = _locked(post)
        if p.status == S.DELETED:
            raise ServiceError("This post has already been deleted.", code="invalid_transition", status_code=409)
        p.status, p.deleted_at, p.deleted_by = S.DELETED, timezone.now(), author
        p.save()
        _event(p, PostEvent.Kind.DELETED, author)
        n, _ = p.notifications.all().delete()
        from apps.moderation.services import author_removed

        author_removed("post", p)  # spec 13 §11: an open case closes as "Removed by author"
    record(A.POST_DELETED, target=p, actor=author, request=request, notifications_deleted=n)
    return p


# ---------------------------------------------------------------- moderation
def _require_admin(actor, candidate):
    """Spec 13 §4.3: moderators and system admins act on posts; nobody linked to the candidate."""
    if not (is_system_admin(actor) or has_role(actor, Role.MODERATOR)):
        raise ServiceError("Moderator access is required.", code="permission_denied", status_code=403)
    if can_post(actor, candidate):
        raise ServiceError("You're linked to this candidate, so someone else has to moderate its posts.", code="self_moderation",
                           status_code=403)


def remove_post(post, admin, reason: str, *, request=None, rule="", note_to_author="", internal_note="", source=None) -> CandidatePost:
    from apps.moderation import services as moderation
    from apps.moderation.rules import RULE_IDS

    reason = (reason or "").strip()
    if not reason:
        raise ServiceError("Give a reason the campaign team will understand.", code="validation_error", status_code=400)
    _require_admin(admin, post.candidate)
    if rule not in RULE_IDS:
        raise ServiceError("Choose the rule it broke.", code="rule_required", status_code=400)
    with transaction.atomic():
        p = _locked(post)
        if p.status != S.PUBLISHED:
            raise ServiceError("Only published posts can be removed.", code="invalid_transition", status_code=409)
        now = timezone.now()
        p.status, p.removed_at, p.removed_by, p.removed_reason, p.removed_rule = S.REMOVED, now, admin, reason[:500], rule
        p.save()
        from apps.media.services import set_hidden

        set_hidden(p.image, True)  # spec 15 §7.7
        _event(p, PostEvent.Kind.REMOVED, admin, reason)
        case = moderation.content_removed("post", p, admin, rule_id=rule, note_to_author=note_to_author, internal_note=internal_note,
                                          source=source)
    record(A.POST_REMOVED, target=p, actor=admin, request=request, rule=rule, case=str(case.pk) if case else "")
    return p


def restore_post(post, admin, *, request=None, note="") -> CandidatePost:
    from apps.moderation import services as moderation

    _require_admin(admin, post.candidate)
    with transaction.atomic():
        p = _locked(post)
        if p.status != S.REMOVED:
            raise ServiceError("Only removed posts can be restored.", code="invalid_transition", status_code=409)
        p.status, p.removed_at, p.removed_by, p.removed_reason, p.removed_rule = S.PUBLISHED, None, None, "", ""
        p.save()
        from apps.media.services import set_hidden

        set_hidden(p.image, False)
        _event(p, PostEvent.Kind.RESTORED, admin, note)
        moderation.content_restored("post", p, admin, note)
    record(A.POST_RESTORED, target=p, actor=admin, request=request)  # no delivery on restore
    return p


def notify_count(candidate) -> int:
    from apps.follows.models import CandidateFollow

    now = timezone.now()
    return CandidateFollow.objects.filter(candidate=candidate, notify=True, notify_changed_at__lte=now, user__is_active=True,
                                          user__clerk_deleted_at__isnull=True).count()
