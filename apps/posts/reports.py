"""Reports on candidate posts — spec 09 §6.2, mirroring 07's news/reports.py."""
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core.exceptions import ServiceError
from apps.news.models import ReportReason, ReportStatus
from apps.news.reports import ip_hash

from .models import CandidatePost, PostEvent, PostReport


def report(post: CandidatePost, reason: str, details: str = "", *, user=None, contact_email="", ip="") -> PostReport:
    if reason not in ReportReason.values:
        raise ServiceError("Choose a reason.", code="validation_error", status_code=400)
    if not CandidatePost.objects.visible().filter(pk=post.pk).exists():
        raise ServiceError("Post not found.", code="not_found", status_code=404)
    details = (details or "").strip()[:1000]
    if user is not None and user.is_authenticated:
        existing = PostReport.objects.filter(post=post, reporter=user, status=ReportStatus.OPEN).first()
        if existing:
            existing.reason, existing.details = reason, details
            existing.save(update_fields=["reason", "details"])
            return existing
        try:
            with transaction.atomic():
                return PostReport.objects.create(post=post, reason=reason, details=details, reporter=user, ip_hash=ip_hash(ip))
        except IntegrityError:
            return report(post, reason, details, user=user, ip=ip)
    return PostReport.objects.create(post=post, reason=reason, details=details, contact_email=(contact_email or "")[:254], ip_hash=ip_hash(ip))


def dismiss(report_obj: PostReport, admin, note: str = "", *, request=None) -> PostReport:
    from .services import _require_admin

    with transaction.atomic():
        r = PostReport.objects.select_for_update(of=("self",)).select_related("post__candidate").get(pk=report_obj.pk)
        _require_admin(admin, r.post.candidate)
        if r.status != ReportStatus.OPEN:
            raise ServiceError("This report has already been resolved.", code="invalid_transition", status_code=409)
        r.status, r.resolved_by, r.resolved_at, r.resolution_note = ReportStatus.DISMISSED, admin, timezone.now(), (note or "")[:300]
        r.save()
        PostEvent.objects.create(post=r.post, event=PostEvent.Kind.REPORT_DISMISSED, actor=admin, note=(note or "")[:300])
    record(AuditLog.Action.POST_REPORT_RESOLVED, target=r.post, actor=admin, request=request, report=str(r.pk), outcome="dismissed")
    return r
