"""
Background job records (spec 11 §5.2, §6.3). Every scheduled command runs inside `track()` so
the admin dashboard can show when it last ran and whether it failed.

    class Command(TrackedCommand):
        job = "prune_news"
        def run(self, *args, **opts) -> str:
            ...
            return "Deleted 3 articles"      # short, fixed phrase with counts; never personal data
"""
from contextlib import contextmanager
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from .models import JobRun

H = timedelta(hours=1)
# job → (words, overdue after; None = run by hand, never overdue)
KNOWN_JOBS = {
    "sweep_payments": ("Recheck stuck payments", 2 * H),
    "sweep_donations": ("Recheck stuck donations", 2 * H),
    "check_donations": ("Check donation records", 26 * H),
    "prune_pvc": ("Clear old VINs", 26 * H),
    "prune_news": ("Clear old news data", 26 * H),
    "prune_notifications": ("Clear old notifications", 26 * H),
    "prune_posts": ("Clear deleted posts", 26 * H),
    "deliver_announcement": ("Resend announcement notifications", None),
    "deliver_post": ("Resend candidate post notifications", None),
    "prune_jobruns": ("Clear old job records", 8 * 24 * H),
    "rebuild_search": ("Rebuild search index", 8 * 24 * H),
    "moderation_tick": ("Moderation upkeep (claims, burst flags, copyright windows)", 1 * H),
    "prune_moderation": ("Clear old moderation records", 26 * H),
    "privacy_tick": ("Privacy upkeep (request and incident clocks)", 2 * H),
    "verify_audit": ("Check the audit log chain", 8 * 24 * H),
    "prune_audit": ("Clear audit and privacy records past 6 years", 8 * 24 * H),
    "restore_drill": ("Record a restore test", None),
    # spec 15
    "process_outbox": ("Run background tasks (notifications, image clean-up, retries)", H / 6),
    "prune_outbox": ("Clear finished background tasks", 26 * H),
    "prune_webhooks": ("Clear webhook receipts past 90 days", 26 * H),
    "media_audit": ("Find and remove orphan images", 8 * 24 * H),
    "migrate_media_to_cloudinary": ("Move local images to Cloudinary", None),
    # spec 16
    "check_integrity": ("Check data integrity", 8 * 24 * H),
    "sync_communities": ("Sync communities from geography", 26 * H),  # spec 17 §12.2
    "prune_communities": ("Clear old community posts", 26 * H),
    "events_tick": ("Event reminders and deadlines", H / 2),  # spec 18 §12.3 (every 15 min)
    "prune_events": ("Clear old event registrations", 26 * H),
    "prune_volunteers": ("Clear finished volunteer applications", 26 * H),
}
MANUAL_STALE = 24 * H  # a by-hand run that hasn't finished after this "didn't finish"
KEEP = timedelta(days=90)


class _Run:
    summary = ""


def _one_line(exc) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}"[:255] if text else type(exc).__name__


@contextmanager
def track(job: str):
    row = JobRun.objects.create(job=job, started_at=timezone.now())
    run = _Run()
    try:
        yield run
    except BaseException as exc:
        row.finished_at, row.ok, row.error = timezone.now(), False, _one_line(exc)
        row.save(update_fields=["finished_at", "ok", "error"])
        raise
    row.finished_at, row.ok, row.summary = timezone.now(), True, str(run.summary or "")[:255]
    row.save(update_fields=["finished_at", "ok", "summary"])


class TrackedCommand(BaseCommand):
    job = ""

    def run(self, *args, **opts) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def handle(self, *args, **opts):
        with track(self.job) as t:
            t.summary = self.run(*args, **opts) or ""
        if t.summary:
            self.stdout.write(t.summary)


def state(row: JobRun | None, job: str, now=None) -> str:
    """never | running | ok | failed | overdue | stalled ("didn't finish")."""
    now = now or timezone.now()
    overdue = KNOWN_JOBS.get(job, ("", None))[1]
    if row is None:
        return "never"
    if row.finished_at is None:
        limit = 2 * overdue if overdue else MANUAL_STALE
        return "stalled" if now - row.started_at > limit else "running"
    if row.ok is False:
        return "failed"
    if overdue and now - row.finished_at > overdue:
        return "overdue"
    return "ok"


STATE_WORDS = {"never": "Never run", "running": "Running", "ok": "Succeeded", "failed": "Failed", "overdue": "Overdue",
               "stalled": "Didn't finish"}


def latest_runs(now=None) -> list[dict]:
    now = now or timezone.now()
    out = []
    latest = {r.job: r for r in JobRun.objects.filter(job__in=list(KNOWN_JOBS)).order_by("job", "-started_at").distinct("job")}
    for job, (label, overdue) in KNOWN_JOBS.items():
        row = latest.get(job)
        st = state(row, job, now)
        out.append({"job": job, "label": label, "state": st, "state_label": STATE_WORDS[st],
                    "manual": overdue is None, "started_at": row.started_at if row else None,
                    "finished_at": row.finished_at if row else None, "summary": row.summary if row else "",
                    "error": row.error if row else ""})
    return out


def prune(now=None) -> int:
    n, _ = JobRun.objects.filter(started_at__lt=(now or timezone.now()) - KEEP).delete()
    return n
