"""
Community news — spec 07 §6.1. Members submit; news reviewers approve or reject; nothing is public before
approval; reviewers never rewrite a member's words and never review their own submission.
"""
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import blackouts
from apps.core.exceptions import ServiceError

from . import images
from .models import Article, ArticleEvent, ArticleStatus as S, Source

A = AuditLog.Action
DECLARATIONS = ("accurate", "no_private_info", "rules")
EDITABLE = {S.PENDING, S.REJECTED}


def _event(article, kind, actor=None, note=""):
    ArticleEvent.objects.create(article=article, event=kind, actor=actor, note=(note or "")[:300])


def _locked(article) -> Article:
    return Article.objects.select_for_update(of=("self",)).get(pk=article.pk)


def clean_text(value: str) -> str:
    """Plain text only: normalise newlines, strip trailing spaces, collapse 3+ blank lines. Never HTML-escaped here."""
    value = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(line.rstrip() for line in value.split("\n")).strip()
    return re.sub(r"\n{3,}", "\n\n", value)


def validate_source_url(url: str):
    if url and not url.lower().startswith("https://"):
        raise serializers.ValidationError({"source_url": ["Use a link that starts with https://"]})


def paused_until():
    w = blackouts.active_window()
    return w["end"] if w else None


def require_not_paused(what="Publishing"):
    until = paused_until()
    if until:
        exc = ServiceError(f"{what} is paused until {timezone.localtime(until):%d %b %Y, %H:%M} (Electoral Act 2026 campaign blackout).",
                           code="publishing_paused", status_code=409)
        exc.extra = {"until": until.isoformat()}
        raise exc


def _check_community(data, partial=False):
    errors = {}
    if not partial or "title" in data:
        if not data.get("title", "").strip():
            errors["title"] = ["Give it a title."]
    if not partial or "body" in data:
        n = len(data.get("body", ""))
        if n < settings.NEWS_COMMUNITY_MIN_BODY:
            errors["body"] = [f"Write at least {settings.NEWS_COMMUNITY_MIN_BODY} characters."]
        elif n > settings.NEWS_COMMUNITY_MAX_BODY:
            errors["body"] = [f"Keep it under {settings.NEWS_COMMUNITY_MAX_BODY:,} characters."]
    if errors:
        raise serializers.ValidationError(errors)
    validate_source_url(data.get("source_url", ""))


# ------------------------------------------------------------------ member
def submit(user, data, *, request=None) -> Article:
    if not user.profile_complete:
        raise ServiceError("Finish setting up your profile first.", code="profile_incomplete", status_code=403)
    from apps.moderation.services import require_not_paused as require_author_not_paused

    require_author_not_paused(user)  # spec 13 §6.6
    data = {**data, "title": data.get("title", "").strip(), "body": clean_text(data.get("body", ""))}
    _check_community(data)
    decl = data.get("declarations") or {}
    missing = [k for k in DECLARATIONS if decl.get(k) is not True]
    if missing:
        raise serializers.ValidationError({"declarations": {k: ["Tick this to continue."] for k in missing}})
    with transaction.atomic():
        # One row lock on the user serialises "3 pending" checks for the same author.
        type(user).objects.select_for_update().filter(pk=user.pk).first()
        if Article.objects.filter(author=user, source=Source.COMMUNITY, status=S.PENDING).count() >= settings.NEWS_MAX_PENDING:
            raise ServiceError(f"You already have {settings.NEWS_MAX_PENDING} articles waiting for review. Wait for a decision first.",
                               code="too_many_pending", status_code=409)
        now = timezone.now()
        a = Article.objects.create(
            source=Source.COMMUNITY, status=S.PENDING, title=data["title"], body=data["body"], state=data.get("state", ""),
            source_url=data.get("source_url", ""), source_name="", author=user, show_author_name=bool(data.get("show_author_name")),
            declarations={**{k: True for k in DECLARATIONS}, "declared_at": now.isoformat()}, submitted_at=now)
        _event(a, ArticleEvent.Kind.SUBMITTED, user)
    record(A.NEWS_SUBMITTED, target=a, actor=user, request=request)
    return a


def edit(article, user, data, *, request=None) -> Article:
    with transaction.atomic():
        a = _locked(article)
        if a.author_id != user.pk or a.source != Source.COMMUNITY:
            raise ServiceError("Article not found.", code="not_found", status_code=404)
        content = {k: data[k] for k in ("title", "body", "state", "source_url") if k in data}
        if "show_author_name" in data and not content:
            return set_byline(a, user, bool(data["show_author_name"]), request=request, locked=True)
        from apps.moderation.services import require_not_paused as require_author_not_paused

        require_author_not_paused(user)  # spec 13 §6.6 (hiding your name stays possible while paused)
        if a.status not in EDITABLE:
            raise ServiceError("You can only edit an article while it's pending or after it was rejected.",
                               code="invalid_transition", status_code=409)
        if "title" in content:
            content["title"] = content["title"].strip()
        if "body" in content:
            content["body"] = clean_text(content["body"])
        _check_community(content, partial=True)
        changed = [k for k, v in content.items() if getattr(a, k) != v]
        for k, v in content.items():
            setattr(a, k, v)
        if "show_author_name" in data:
            a.show_author_name = bool(data["show_author_name"])
        if a.status == S.REJECTED:
            a.status, a.submitted_at = S.PENDING, timezone.now()
            a.decided_by, a.decided_at, a.decision_reason = None, None, ""
            _event(a, ArticleEvent.Kind.RESUBMITTED, user)
        elif changed:
            _event(a, ArticleEvent.Kind.EDITED, user, ", ".join(changed))
        a.save()
    record(A.NEWS_EDITED, target=a, actor=user, request=request, fields=changed)
    return a


def set_byline(article, user, show_name: bool, *, request=None, locked=False) -> Article:
    with transaction.atomic():
        a = article if locked else _locked(article)
        if a.author_id != user.pk or a.source != Source.COMMUNITY:
            raise ServiceError("Article not found.", code="not_found", status_code=404)
        if a.show_author_name != show_name:
            a.show_author_name = show_name
            a.save(update_fields=["show_author_name", "updated_at"])
            _event(a, ArticleEvent.Kind.BYLINE_CHANGED, user, "name shown" if show_name else "a member")
    return a


def withdraw(article, user, *, request=None) -> Article:
    with transaction.atomic():
        a = _locked(article)
        if a.author_id != user.pk or a.source != Source.COMMUNITY:
            raise ServiceError("Article not found.", code="not_found", status_code=404)
        if a.status in (S.WITHDRAWN, S.REMOVED):
            raise ServiceError("This article can't be withdrawn.", code="invalid_transition", status_code=409)
        prev = a.status
        a.status = S.WITHDRAWN
        a.pinned_at = None
        a.save()
        _event(a, ArticleEvent.Kind.WITHDRAWN, user, f"from {prev}")
    record(A.NEWS_WITHDRAWN, target=a, actor=user, request=request, previous=prev)
    return a


def set_image(article, user, upload, *, rights_confirmed: bool, request=None) -> Article:
    if not rights_confirmed:
        raise serializers.ValidationError({"rights_confirmed": ["Confirm you took this photo or have the right to share it."]})
    stored = images.store(upload, purpose="announcement_image" if article.source == Source.OFFICIAL else "news_image", user=user)
    from apps.media.services import asset_for_name
    with transaction.atomic():
        a = _locked(article)
        allowed = (a.author_id == user.pk and a.status in EDITABLE) if a.source == Source.COMMUNITY else True
        if not allowed:
            images.delete(stored["image"], stored["image_thumb"])
            raise ServiceError("You can only change the image while the article is pending or rejected.",
                               code="invalid_transition", status_code=409)
        old = (a.image, a.image_thumb)
        for k, v in stored.items():
            setattr(a, k, v)
        a.image_asset = asset_for_name(stored["image"])
        if a.status == S.SUCCESSFUL:
            a.edited_at = timezone.now()
        a.save()
        _event(a, ArticleEvent.Kind.EDITED, user, "image")
    images.delete(*old)
    return a


def remove_image(article, user, *, request=None) -> Article:
    with transaction.atomic():
        a = _locked(article)
        if a.source == Source.COMMUNITY and not (a.author_id == user.pk and a.status in EDITABLE):
            raise ServiceError("You can only change the image while the article is pending or rejected.",
                               code="invalid_transition", status_code=409)
        old = (a.image, a.image_thumb)
        a.image = a.image_thumb = ""
        a.image_width = a.image_height = None
        a.image_asset = None
        if a.status == S.SUCCESSFUL:
            a.edited_at = timezone.now()
        a.save()
        if any(old):
            _event(a, ArticleEvent.Kind.EDITED, user, "image removed")
    images.delete(*old)
    return a


# ---------------------------------------------------------------- reviewer
def _no_self(a, reviewer):
    if a.author_id and a.author_id == reviewer.pk:
        raise ServiceError("You can't review your own submission.", code="self_review", status_code=403)


def approve(article, reviewer, *, request=None) -> Article:
    from apps.notifications.services import notify_decision

    with transaction.atomic():
        a = _locked(article)
        if a.source != Source.COMMUNITY or a.status != S.PENDING:
            raise ServiceError("This article has already been decided.", code="invalid_transition", status_code=409)
        _no_self(a, reviewer)
        require_not_paused("Approving community news")
        now = timezone.now()
        a.status, a.decided_by, a.decided_at, a.decision_reason = S.SUCCESSFUL, reviewer, now, ""
        a.published_at = a.published_at or now
        a.save()
        _event(a, ArticleEvent.Kind.APPROVED, reviewer)
        notify_decision(a, "approved")
    record(A.NEWS_APPROVED, target=a, actor=reviewer, request=request)
    return a


def reject(article, reviewer, reason: str, *, request=None) -> Article:
    from apps.notifications.services import notify_decision

    reason = (reason or "").strip()
    if not reason:
        raise ServiceError("Give a reason the author can act on.", code="validation_error", status_code=400)
    with transaction.atomic():
        a = _locked(article)
        if a.source != Source.COMMUNITY or a.status != S.PENDING:
            raise ServiceError("This article has already been decided.", code="invalid_transition", status_code=409)
        _no_self(a, reviewer)
        a.status, a.decided_by, a.decided_at, a.decision_reason = S.REJECTED, reviewer, timezone.now(), reason[:500]
        a.save()
        _event(a, ArticleEvent.Kind.REJECTED, reviewer, reason)
        notify_decision(a, "rejected")
    record(A.NEWS_REJECTED, target=a, actor=reviewer, request=request)
    return a


def remove(article, actor, reason: str, *, request=None, rule="", note_to_author="", internal_note="", source=None) -> Article:
    """Spec 13 §6.2: every removal cites a rule and goes through moderation's case, except an editorial
    unpublish of an official announcement that nobody reported (no rule, no public notice)."""
    from apps.moderation import services as moderation
    from apps.moderation.models import Case, CaseStatus
    from apps.moderation.rules import RULE_IDS

    reason = (reason or "").strip()
    if not reason:
        raise ServiceError("Give a reason for removing it.", code="validation_error", status_code=400)
    if rule and rule not in RULE_IDS:
        raise ServiceError("Choose the rule it broke.", code="rule_required", status_code=400)
    with transaction.atomic():
        a = _locked(article)
        if a.status != S.SUCCESSFUL:
            raise ServiceError("Only published articles can be removed.", code="invalid_transition", status_code=409)
        if a.source == Source.COMMUNITY:
            _no_self(a, actor)
        reported = Case.objects.filter(target_kind="article", target_id=a.pk).exclude(status=CaseStatus.CLOSED).exists()
        if not rule and (a.source == Source.COMMUNITY or reported):
            raise ServiceError("Choose the rule it broke.", code="rule_required", status_code=400)
        now = timezone.now()
        a.status, a.removed_by, a.removed_at, a.removed_reason, a.pinned_at = S.REMOVED, actor, now, reason[:500], None
        a.removed_rule = rule
        a.save()
        from apps.media.services import set_hidden

        set_hidden(a.image, True)  # spec 15 §7.7: the old image URL stops working
        kind = ArticleEvent.Kind.UNPUBLISHED if a.source == Source.OFFICIAL and not rule else ArticleEvent.Kind.REMOVED
        _event(a, kind, actor, reason)
        case = moderation.content_removed("article", a, actor, rule_id=rule, note_to_author=note_to_author, internal_note=internal_note,
                                          source=source) if rule else None
    record(A.ANNOUNCEMENT_UNPUBLISHED if a.source == Source.OFFICIAL else A.NEWS_REMOVED, target=a, actor=actor, request=request,
           rule=rule, case=str(case.pk) if case else "")
    return a


def restore(article, actor, *, request=None, note="") -> Article:
    from apps.moderation import services as moderation

    with transaction.atomic():
        a = _locked(article)
        if a.status != S.REMOVED:
            raise ServiceError("Only removed articles can be restored.", code="invalid_transition", status_code=409)
        if a.source == Source.OFFICIAL and a.category == "campaign":
            require_not_paused("Publishing campaign announcements")
        a.status, a.removed_by, a.removed_at, a.removed_reason, a.removed_rule = S.SUCCESSFUL, None, None, "", ""
        a.save()
        from apps.media.services import set_hidden

        set_hidden(a.image, False)
        _event(a, ArticleEvent.Kind.RESTORED, actor, note)
        moderation.content_restored("article", a, actor, note)
    record(A.ANNOUNCEMENT_RESTORED if a.source == Source.OFFICIAL else A.NEWS_RESTORED, target=a, actor=actor, request=request)
    return a


# ------------------------------------------------------------- lifecycle
def erase_for_deleted_user(user) -> dict:
    """Spec 07 §6.5 — called inside accounts.mark_deleted's transaction."""
    drafts = Article.objects.filter(author=user, source=Source.COMMUNITY, status__in=[S.PENDING, S.REJECTED])
    old_images = list(drafts.values_list("image", "image_thumb"))
    n_drafts = drafts.update(status=S.WITHDRAWN, title="", body="", source_url="", image="", image_thumb="", image_width=None,
                             image_height=None, image_asset=None, declarations={})
    n_bylines = Article.objects.filter(author=user, source=Source.COMMUNITY, show_author_name=True).update(show_author_name=False)
    from apps.moderation.services import erase_for_deleted_user as erase_reports

    n_reports = erase_reports(user)["reports_detached"]
    transaction.on_commit(lambda: [images.delete(*pair) for pair in old_images])
    from apps.notifications.services import erase_for_deleted_user as erase_notifications

    n_notifications = erase_notifications(user)
    return {"drafts_cleared": n_drafts, "bylines_hidden": n_bylines, "reports_detached": n_reports, "notifications_deleted": n_notifications}
