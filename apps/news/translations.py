"""
Official announcements in several languages (spec 21 §10). A person writes each version (never a machine); a second
system admin reviews and publishes it. The image and source link are the original's. Editing the original makes
published versions "out of date" (they stay, with a note) until someone updates them.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from .models import Article, ArticleTranslation, Source
from .models import TranslationStatus as TS
from .services import clean_text

A = AuditLog.Action
VERSION_LANGUAGES = ("en", "ha", "ig", "yo", "pcm")


def _original_changed_at(a: Article):
    return a.edited_at or a.published_at or a.created_at


def out_of_date(t: ArticleTranslation, a: Article | None = None) -> bool:
    a = a or t.article
    return _original_changed_at(a) > t.original_updated_at


def payload(t: ArticleTranslation, a: Article | None = None) -> dict:
    return {"language": t.language, "title": t.title, "body": t.body, "status": t.status, "out_of_date": out_of_date(t, a),
            "translated_by": getattr(t.translated_by, "full_name", "") or "", "reviewed_by": getattr(t.reviewed_by, "full_name", "") or "",
            "published_at": t.published_at, "updated_at": t.updated_at}


def public_versions(a: Article, lang: str) -> dict:
    """For the public article: the published versions, and (with ?lang=) that version's title and body."""
    if a.source != Source.OFFICIAL:
        return {"translations": [], "version_language": a.language}
    published = list(a.translations.filter(status=TS.PUBLISHED))
    out = {"translations": [{"language": t.language, "title": t.title, "updated_at": t.updated_at, "out_of_date": out_of_date(t, a)}
                            for t in published],
           "version_language": a.language, "version_out_of_date": False}
    chosen = next((t for t in published if t.language == lang), None)
    if chosen:
        out |= {"title": chosen.title, "body": chosen.body, "version_language": chosen.language,
                "version_out_of_date": out_of_date(chosen, a)}
    return out


def _official(pk) -> Article:
    a = Article.objects.filter(pk=pk, source=Source.OFFICIAL).first()
    if a is None:
        raise ServiceError("Announcement not found.", code="not_found", status_code=404)
    return a


def _language(a: Article, lang: str) -> str:
    if lang not in VERSION_LANGUAGES:
        raise ServiceError("Choose English, Hausa, Igbo, Yoruba or Nigerian Pidgin.", code="language_invalid", status_code=400)
    if lang == a.language:
        raise ServiceError("That is the announcement's own language; edit the original instead.", code="translation_same_language",
                           status_code=400)
    return lang


def save(article_id, lang: str, user, data: dict, *, request=None) -> ArticleTranslation:
    from django.conf import settings

    a = _official(article_id)
    lang = _language(a, lang)
    title, body = (data.get("title") or "").strip(), clean_text(data.get("body") or "")
    errors = {}
    if not title or len(title) > 150:
        errors["title"] = ["Give it a title (up to 150 characters)."]
    if not body:
        errors["body"] = ["Write the announcement."]
    elif len(body) > settings.NEWS_OFFICIAL_MAX_BODY:
        errors["body"] = [f"Keep it under {settings.NEWS_OFFICIAL_MAX_BODY:,} characters."]
    if errors:
        raise serializers.ValidationError(errors)
    with transaction.atomic():
        t = ArticleTranslation.objects.select_for_update().filter(article=a, language=lang).first()
        if t is None:
            t = ArticleTranslation(article=a, language=lang)
        # A changed version goes back to draft: a second person reviews what the readers will see (rule 5).
        t.title, t.body, t.translated_by, t.status = title, body, user, TS.DRAFT
        t.reviewed_by, t.published_at, t.original_updated_at = None, None, _original_changed_at(a)
        t.save()
    record(A.TRANSLATION_SAVED, target=a, actor=user, request=request, language=lang)
    _reindex(a)
    return t


def publish(article_id, lang: str, user, *, request=None) -> ArticleTranslation:
    a = _official(article_id)
    with transaction.atomic():
        t = ArticleTranslation.objects.select_for_update().filter(article=a, language=lang).first()
        if t is None:
            raise ServiceError("There is no version in that language yet.", code="not_found", status_code=404)
        if t.translated_by_id == user.pk:
            raise ServiceError("Someone else must review and publish a version you wrote.", code="translation_self_review",
                               status_code=409)
        t.status, t.reviewed_by, t.published_at = TS.PUBLISHED, user, timezone.now()
        t.original_updated_at = max(t.original_updated_at, _original_changed_at(a)) if not out_of_date(t, a) else t.original_updated_at
        t.save()
    record(A.TRANSLATION_PUBLISHED, target=a, actor=user, request=request, language=lang)
    _reindex(a)
    return t


def delete(article_id, lang: str, user, *, request=None):
    a = _official(article_id)
    n, _ = ArticleTranslation.objects.filter(article=a, language=lang).delete()
    if not n:
        raise ServiceError("There is no version in that language.", code="not_found", status_code=404)
    record(A.TRANSLATION_DELETED, target=a, actor=user, request=request, language=lang)
    _reindex(a)


def _reindex(a):
    from apps.search.index import index_article

    index_article(a)
