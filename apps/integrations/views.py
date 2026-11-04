"""/admin/integrations (spec 15 §15.3). System admins. Names, states and counts only: no payloads, no settings values."""
from datetime import timedelta

from django.db.models import Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.cache import add_never_cache_headers, patch_cache_control
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as S
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.access.permissions import IsSystemAdmin
from apps.access.views import AdminReadThrottle, AdminWriteThrottle
from apps.audit.models import AuditLog
from apps.audit.services import record
from apps.core import outbox
from apps.core.exceptions import ServiceError
from apps.core.models import OutboxJob

from . import registry
from .models import WebhookReceipt

PROVIDERS = ("clerk", "paystack", "fake")


SERVICE = inline_serializer("IntegrationService", {
    "key": S.CharField(), "name": S.CharField(), "purpose": S.CharField(), "settings": S.ListField(child=S.CharField()),
    "timeouts": S.CharField(), "data_sent": S.CharField(), "in_use": S.BooleanField(), "configured": S.BooleanField(),
    "state": S.ChoiceField(["ok", "failing", "unavailable", "not_configured", "not_in_use"]),
    "last_success": S.CharField(allow_null=True), "last_failure": S.CharField(allow_null=True), "last_reason": S.CharField(),
    "circuit_open": S.BooleanField()})
RECEIPT = inline_serializer("WebhookReceipt", {
    "id": S.UUIDField(), "provider": S.CharField(), "event_type": S.CharField(), "status": S.ChoiceField(WebhookReceipt.Status.values),
    "signature_valid": S.BooleanField(), "attempts": S.IntegerField(), "error": S.CharField(), "request_id": S.CharField(),
    "received_at": S.DateTimeField(), "processed_at": S.DateTimeField(allow_null=True)})
JOB = inline_serializer("OutboxJob", {
    "id": S.UUIDField(), "kind": S.CharField(), "status": S.ChoiceField(OutboxJob.Status.values), "attempts": S.IntegerField(),
    "max_attempts": S.IntegerField(), "next_attempt_at": S.DateTimeField(), "last_error": S.CharField(), "request_id": S.CharField(),
    "created_at": S.DateTimeField(), "finished_at": S.DateTimeField(allow_null=True), "stuck": S.BooleanField()})
OVERVIEW = inline_serializer("IntegrationsOverview", {
    "generated_at": S.DateTimeField(), "services": S.ListField(child=SERVICE),
    "webhooks": S.ListField(child=inline_serializer("WebhookCounts", {
        "provider": S.CharField(), "processed": S.IntegerField(), "ignored": S.IntegerField(), "failed": S.IntegerField(),
        "bad_signatures": S.IntegerField()})),
    "outbox": inline_serializer("OutboxStats", {
        "pending": S.IntegerField(), "failed": S.IntegerField(), "oldest_due_at": S.DateTimeField(allow_null=True),
        "oldest_due_age_s": S.IntegerField(), "oldest_failed_at": S.DateTimeField(allow_null=True), "failed_by_kind": S.DictField(child=S.IntegerField()), "stuck": S.BooleanField()})})


def _page(name, child):
    return inline_serializer(name, {"count": S.IntegerField(), "next": S.CharField(allow_null=True), "previous": S.CharField(allow_null=True),
                                    "results": S.ListField(child=child)})


class Page20(PageNumberPagination):
    page_size = 20


def invalid_filter(name):
    return ServiceError(f"Unknown value for {name}.", code="invalid_filter", status_code=400)


class _Admin(APIView):
    permission_classes = [IsSystemAdmin]

    def get_throttles(self):
        return [AdminWriteThrottle()] if self.request.method == "POST" else [AdminReadThrottle()]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        patch_cache_control(response, private=True, no_store=True)
        return response


def receipt_row(r):
    return {"id": str(r.pk), "provider": r.provider, "event_type": r.event_type, "status": r.status,
            "signature_valid": r.signature_valid, "attempts": r.attempts, "error": r.error, "request_id": r.request_id,
            "received_at": r.received_at, "processed_at": r.processed_at}


def job_row(j, now):
    stuck = j.status == OutboxJob.Status.PENDING and j.next_attempt_at <= now - outbox.STUCK_AFTER
    return {"id": str(j.pk), "kind": j.kind, "status": j.status, "attempts": j.attempts, "max_attempts": outbox.MAX_ATTEMPTS,
            "next_attempt_at": j.next_attempt_at, "last_error": j.last_error, "request_id": j.request_id,
            "created_at": j.created_at, "finished_at": j.finished_at, "stuck": stuck}


class Overview(_Admin):
    @extend_schema(summary="External services, webhooks (24 h) and background tasks", responses=OVERVIEW)
    def get(self, request):
        now = timezone.now()
        since = now - timedelta(hours=24)
        counts = {}
        for row in (WebhookReceipt.objects.filter(received_at__gte=since).values("provider", "status").annotate(n=Count("id"))):
            counts.setdefault(row["provider"], {"processed": 0, "ignored": 0, "failed": 0})[row["status"]] = row["n"]
        bad = dict(WebhookReceipt.objects.filter(received_at__gte=since, signature_valid=False)
                   .values_list("provider").annotate(n=Count("id")))
        st = outbox.stats(now)
        by_kind = dict(OutboxJob.objects.filter(status=OutboxJob.Status.FAILED).values_list("kind").annotate(n=Count("id")))
        return Response({"generated_at": now, "services": registry.snapshot(),
                         "webhooks": [{"provider": p, **c, "bad_signatures": bad.get(p, 0)} for p, c in sorted(counts.items())],
                         "outbox": {**st, "failed_by_kind": by_kind,
                                    "stuck": st["oldest_due_age_s"] >= outbox.STUCK_AFTER.total_seconds()}})


class Webhooks(_Admin):
    @extend_schema(summary="Webhook receipts, newest first (no payloads)", responses=_page("WebhookReceiptPage", RECEIPT),
                   parameters=[OpenApiParameter("provider", enum=list(PROVIDERS)),
                               OpenApiParameter("status", enum=WebhookReceipt.Status.values)])
    def get(self, request):
        qs = WebhookReceipt.objects.all()
        provider, status = request.query_params.get("provider", ""), request.query_params.get("status", "")
        if provider:
            if provider not in PROVIDERS:
                raise invalid_filter("provider")
            qs = qs.filter(provider=provider)
        if status:
            if status not in WebhookReceipt.Status.values:
                raise invalid_filter("status")
            qs = qs.filter(status=status)
        p = Page20()
        page = p.paginate_queryset(qs.order_by("-received_at", "-id"), request, view=self)
        return p.get_paginated_response([receipt_row(r) for r in page])


class Outbox(_Admin):
    @extend_schema(summary="Background tasks by status (default: failed)", responses=_page("OutboxJobPage", JOB),
                   parameters=[OpenApiParameter("status", enum=[*OutboxJob.Status.values, "stuck"], default="failed")])
    def get(self, request):
        now = timezone.now()
        status = request.query_params.get("status", "failed")
        qs = OutboxJob.objects.all()
        if status == "stuck":
            qs = qs.filter(status=OutboxJob.Status.PENDING, next_attempt_at__lte=now - outbox.STUCK_AFTER)
        elif status in OutboxJob.Status.values:
            qs = qs.filter(status=status)
        else:
            raise invalid_filter("status")
        p = Page20()
        page = p.paginate_queryset(qs.order_by("-created_at", "-id"), request, view=self)
        return p.get_paginated_response([job_row(j, now) for j in page])


class OutboxRetry(_Admin):
    @extend_schema(summary="Retry a failed or waiting background task (audited)", request=None, responses=JOB)
    def post(self, request, pk):
        job = get_object_or_404(OutboxJob, pk=pk)
        if job.status not in (OutboxJob.Status.FAILED, OutboxJob.Status.PENDING):
            raise ServiceError("Only failed or waiting tasks can be retried.", code="invalid_transition", status_code=409)
        before = job.attempts
        job = outbox.retry(job.pk)
        record(AuditLog.Action.INTEGRATION_JOB_RETRIED, target=job, actor=request.user, request=request, kind=job.kind, attempts=before)
        return Response(job_row(job, timezone.now()))
