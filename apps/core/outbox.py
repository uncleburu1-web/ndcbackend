"""
The transactional outbox (spec 15 §9.3).

    from apps.core import outbox
    outbox.enqueue("media_destroy", {"asset": str(asset.pk)}, dedupe_key=f"media_destroy:{asset.pk}")

`enqueue` must be called inside the transaction that makes the change; if that transaction rolls back, the job goes
too. `process_outbox` (cron every minute, or `--loop`) claims due jobs with SKIP LOCKED and runs their handler.
Failures retry after 1 min, 5 min, 30 min, 2 h and 12 h, then the job is `failed` and shows on the dashboard.
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .logging import _scrub
from .models import OutboxJob
from .requestid import current as current_request_id
from .requestid import set_current

logger = logging.getLogger(__name__)

BACKOFF = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=12)]
MAX_ATTEMPTS = len(BACKOFF) + 1
STUCK_AFTER = timedelta(minutes=15)
_HANDLERS = {}


def handler(kind):
    def wrap(fn):
        _HANDLERS[kind] = fn
        return fn

    return wrap


def handlers():
    _load()
    return dict(_HANDLERS)


def _load():
    # Each app registers its handlers in its own `outbox_handlers` module.
    import importlib

    for mod in ("apps.notifications.outbox_handlers", "apps.media.outbox_handlers", "apps.integrations.outbox_handlers",
                "apps.communities.outbox_handlers",
                "apps.accounts.outbox_handlers"):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            pass


def enqueue(kind, payload=None, *, dedupe_key="", delay=None, kick=None) -> OutboxJob | None:
    """Write a job in the caller's transaction. A pending job with the same dedupe_key makes this a no-op.

    `kick` (default: when there is no delay) also tries the job once right after the commit, in this process, so
    the usual case is as quick as the old `on_commit`; if that try fails or the process dies, `process_outbox`
    picks it up."""
    payload = payload or {}
    try:
        with transaction.atomic():
            job = OutboxJob.objects.create(kind=kind, payload=payload, dedupe_key=dedupe_key[:128], request_id=current_request_id(),
                                           next_attempt_at=timezone.now() + (delay or timedelta(0)))
    except IntegrityError:
        return None  # already queued
    if kick if kick is not None else delay is None:
        transaction.on_commit(lambda: _kick(job.pk))
    return job


def _kick(job_id):
    try:
        with transaction.atomic():
            job = (OutboxJob.objects.select_for_update(skip_locked=True)
                   .filter(pk=job_id, status=OutboxJob.Status.PENDING).first())
            if job is None:
                return
            OutboxJob.objects.filter(pk=job.pk).update(status=OutboxJob.Status.RUNNING)
        run_job(job)
    except Exception:  # never into the request that committed; process_outbox will retry
        logger.warning("Outbox kick for %s failed", job_id, exc_info=True)


def _claim(limit, now):
    with transaction.atomic():
        jobs = list(OutboxJob.objects.select_for_update(skip_locked=True)
                    .filter(status=OutboxJob.Status.PENDING, next_attempt_at__lte=now).order_by("next_attempt_at")[:limit])
        ids = [j.pk for j in jobs]
        OutboxJob.objects.filter(pk__in=ids).update(status=OutboxJob.Status.RUNNING)
    return jobs


def run_job(job: OutboxJob) -> bool:
    _load()
    fn = _HANDLERS.get(job.kind)
    token = set_current(job.request_id or "")
    now = timezone.now()
    try:
        if fn is None:
            raise RuntimeError(f"No handler for {job.kind}")
        fn(job.payload)
    except Exception as exc:  # one scrubbed line; the full trace is in the log
        logger.warning("Outbox job %s (%s) failed", job.pk, job.kind, exc_info=True)
        attempts = job.attempts + 1
        line = _scrub(f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")[:255]
        if attempts >= MAX_ATTEMPTS:
            OutboxJob.objects.filter(pk=job.pk).update(status=OutboxJob.Status.FAILED, attempts=attempts, last_error=line, finished_at=now)
        else:
            OutboxJob.objects.filter(pk=job.pk).update(status=OutboxJob.Status.PENDING, attempts=attempts, last_error=line,
                                                       next_attempt_at=now + BACKOFF[attempts - 1])
        return False
    finally:
        from .requestid import _current

        _current.reset(token)
    OutboxJob.objects.filter(pk=job.pk).update(status=OutboxJob.Status.DONE, attempts=job.attempts + 1, finished_at=timezone.now(),
                                               last_error="")
    return True


def process(limit=100, now=None) -> dict:
    now = now or timezone.now()
    # jobs left `running` by a crashed worker go back to pending
    OutboxJob.objects.filter(status=OutboxJob.Status.RUNNING, next_attempt_at__lt=now - STUCK_AFTER).update(status=OutboxJob.Status.PENDING)
    done = failed = 0
    for job in _claim(limit, now):
        if run_job(job):
            done += 1
        else:
            failed += 1
    return {"done": done, "failed": failed}


def retry(job_id) -> OutboxJob:
    job = OutboxJob.objects.get(pk=job_id)
    if job.status in (OutboxJob.Status.FAILED, OutboxJob.Status.PENDING):
        OutboxJob.objects.filter(pk=job.pk).update(status=OutboxJob.Status.PENDING, next_attempt_at=timezone.now(),
                                                   attempts=0 if job.status == OutboxJob.Status.FAILED else job.attempts)
    job.refresh_from_db()
    return job


_STATS_MEMO = {}


def stats(now=None) -> dict:
    """One query: pending and failed counts, the oldest due job, and the oldest failure. Within one request, callers
    passing the same `now` (the admin home's readiness and its card) share the answer."""
    key = (now, current_request_id()) if now is not None and current_request_id() else None
    if key and _STATS_MEMO.get("key") == key:
        return _STATS_MEMO["value"]
    out = _stats(now or timezone.now())
    if key:
        _STATS_MEMO.update(key=key, value=out)
    return out


def _stats(now):
    from django.db.models import Count, Min, Q

    P, F = OutboxJob.Status.PENDING, OutboxJob.Status.FAILED
    agg = OutboxJob.objects.filter(status__in=[P, F]).aggregate(
        pending=Count("id", filter=Q(status=P)), failed=Count("id", filter=Q(status=F)),
        due=Min("next_attempt_at", filter=Q(status=P, next_attempt_at__lte=now)), first_failed=Min("finished_at", filter=Q(status=F)))
    due = agg["due"]
    return {"pending": agg["pending"], "failed": agg["failed"], "oldest_due_at": due,
            "oldest_due_age_s": int((now - due).total_seconds()) if due else 0, "oldest_failed_at": agg["first_failed"]}


def prune(now=None) -> int:
    now = now or timezone.now()
    a, _ = OutboxJob.objects.filter(status=OutboxJob.Status.DONE, finished_at__lt=now - timedelta(days=30)).delete()
    b, _ = OutboxJob.objects.filter(status=OutboxJob.Status.FAILED, finished_at__lt=now - timedelta(days=90)).delete()
    return a + b
