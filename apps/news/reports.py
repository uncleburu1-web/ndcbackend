"""Reports on published articles — spec 07 §6.3 (the NITDA Code complaints channel)."""
import hashlib
import hmac

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError

from .models import Article, ArticleReport, ReportReason, ReportStatus


def ip_hash(ip: str) -> str:
    if not ip:
        return ""
    return hmac.new(settings.SECRET_KEY.encode(), ip.encode(), hashlib.sha256).hexdigest()


def report(article: Article, reason: str, details: str = "", *, user=None, contact_email="", ip="") -> ArticleReport:
    if reason not in ReportReason.values:
        raise ServiceError("Choose a reason.", code="validation_error", status_code=400)
    if article.status != "successful":
        raise ServiceError("Article not found.", code="not_found", status_code=404)
    details = (details or "").strip()[:1000]
    if user is not None and user.is_authenticated:
        existing = ArticleReport.objects.filter(article=article, reporter=user, status=ReportStatus.OPEN).first()
        if existing:
            existing.reason, existing.details = reason, details
            existing.save(update_fields=["reason", "details"])
            return existing
        try:
            with transaction.atomic():
                return ArticleReport.objects.create(article=article, reason=reason, details=details, reporter=user,
                                                    ip_hash=ip_hash(ip))
        except IntegrityError:  # a parallel duplicate — update the open one
            return report(article, reason, details, user=user, ip=ip)
    return ArticleReport.objects.create(article=article, reason=reason, details=details, contact_email=(contact_email or "")[:254],
                                        ip_hash=ip_hash(ip))


def dismiss(report_obj: ArticleReport, reviewer, note: str = "", *, request=None) -> ArticleReport:
    with transaction.atomic():
        r = ArticleReport.objects.select_for_update(of=("self",)).select_related("article").get(pk=report_obj.pk)
        if r.status != ReportStatus.OPEN:
            raise ServiceError("This report has already been resolved.", code="invalid_transition", status_code=409)
        if r.article.author_id and r.article.author_id == reviewer.pk and r.article.source == "community":
            raise ServiceError("You can't resolve reports about your own submission.", code="self_review", status_code=403)
        r.status, r.resolved_by, r.resolved_at, r.resolution_note = ReportStatus.DISMISSED, reviewer, timezone.now(), (note or "")[:300]
        r.save()
    record(AuditLog.Action.NEWS_REPORT_RESOLVED, target=r.article, actor=reviewer, request=request, report=str(r.pk), outcome="dismissed")
    return r
