"""Official announcements — spec 07 §6.2. System admins publish directly; edits never notify again."""
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from .models import Article, ArticleEvent, ArticleStatus as S, Category, Source
from .services import _event, _locked, clean_text, require_not_paused, validate_source_url

A = AuditLog.Action
CONTENT = ("category", "title", "body", "state", "source_name", "source_url")


def _validate(data: dict, partial=False):
    errors = {}
    if not partial or "category" in data:
        if data.get("category") not in Category.values:
            errors["category"] = ["Choose a category."]
    if not partial or "title" in data:
        if not data.get("title", "").strip():
            errors["title"] = ["Give it a title."]
    if not partial or "body" in data:
        body = data.get("body", "")
        if not body:
            errors["body"] = ["Write the announcement."]
        elif len(body) > settings.NEWS_OFFICIAL_MAX_BODY:
            errors["body"] = [f"Keep it under {settings.NEWS_OFFICIAL_MAX_BODY:,} characters."]
    if errors:
        raise serializers.ValidationError(errors)
    validate_source_url(data.get("source_url", ""))


def _source_rule(category, source_name, source_url):
    if category == Category.ELECTION_UPDATES and not (source_name.strip() and source_url):
        exc = serializers.ValidationError({"source_name": ["Official Election Updates need a source (e.g. INEC)."],
                                           "source_url": ["Add the link to the source."]})
        exc.envelope_code = "source_required"
        exc.envelope_message = "Official Election Updates need a source name and link."
        raise exc


def publish(admin, data: dict, *, notify=False, request=None) -> Article:
    data = {**data, "title": data.get("title", "").strip(), "body": clean_text(data.get("body", ""))}
    _validate(data)
    _source_rule(data["category"], data.get("source_name", ""), data.get("source_url", ""))
    if data["category"] == Category.CAMPAIGN:
        require_not_paused("Publishing campaign announcements")
    with transaction.atomic():
        now = timezone.now()
        a = Article.objects.create(
            source=Source.OFFICIAL, status=S.SUCCESSFUL, category=data["category"], title=data["title"], body=data["body"],
            state=data.get("state", ""), source_name=data.get("source_name", "").strip(), source_url=data.get("source_url", ""),
            author=admin, important=bool(notify), published_at=now)
        _event(a, ArticleEvent.Kind.PUBLISHED, admin, "notify everyone" if notify else "")
        if notify:
            from apps.notifications.services import deliver_announcement_on_commit

            deliver_announcement_on_commit(a)
    record(A.ANNOUNCEMENT_PUBLISHED, target=a, actor=admin, request=request, category=a.category, notify=bool(notify))
    return a


def update(article, admin, data: dict, *, request=None) -> Article:
    content = {k: data[k] for k in CONTENT if k in data}
    if "title" in content:
        content["title"] = content["title"].strip()
    if "body" in content:
        content["body"] = clean_text(content["body"])
    if "source_name" in content:
        content["source_name"] = content["source_name"].strip()
    _validate(content, partial=True)
    with transaction.atomic():
        a = _locked(article)
        if a.source != Source.OFFICIAL:
            raise ServiceError("Article not found.", code="not_found", status_code=404)
        merged = {k: content.get(k, getattr(a, k)) for k in CONTENT}
        _source_rule(merged["category"], merged["source_name"], merged["source_url"])
        if Category.CAMPAIGN in (a.category, merged["category"]):
            require_not_paused("Editing campaign announcements")
        changed = [k for k, v in content.items() if getattr(a, k) != v]
        for k, v in content.items():
            setattr(a, k, v)
        if changed:
            a.edited_at = timezone.now()
            a.save()
            _event(a, ArticleEvent.Kind.EDITED, admin, ", ".join(changed))
    if changed:
        record(A.ANNOUNCEMENT_UPDATED, target=a, actor=admin, request=request, fields=changed)
    return a


def pin(article, admin, *, request=None) -> Article:
    with transaction.atomic():
        # Lock every pinned row so two admins can't both pin a 4th.
        pinned = list(Article.objects.select_for_update().filter(source=Source.OFFICIAL, pinned_at__isnull=False).values_list("pk", flat=True))
        a = _locked(article)
        if a.source != Source.OFFICIAL or a.status != S.SUCCESSFUL:
            raise ServiceError("Only published official announcements can be pinned.", code="invalid_transition", status_code=409)
        if a.pinned_at:
            return a
        if len(pinned) >= settings.NEWS_MAX_PINNED:
            raise ServiceError(f"Up to {settings.NEWS_MAX_PINNED} announcements can be pinned. Unpin one first.",
                               code="too_many_pinned", status_code=409)
        a.pinned_at = timezone.now()
        a.save(update_fields=["pinned_at", "updated_at"])
        _event(a, ArticleEvent.Kind.PINNED, admin)
    record(A.ANNOUNCEMENT_PINNED, target=a, actor=admin, request=request, pinned=True)
    return a


def unpin(article, admin, *, request=None) -> Article:
    with transaction.atomic():
        a = _locked(article)
        if a.pinned_at:
            a.pinned_at = None
            a.save(update_fields=["pinned_at", "updated_at"])
            _event(a, ArticleEvent.Kind.UNPINNED, admin)
            record(A.ANNOUNCEMENT_PINNED, target=a, actor=admin, request=request, pinned=False)
    return a
