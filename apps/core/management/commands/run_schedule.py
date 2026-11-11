"""One long-running worker for every background job (deployment on Railway; spec 22 will formalise hosting).

Railway's cron services run at most every 5 minutes and each is billed as its own service, while the outbox needs a
poll every few seconds. So one worker process:
  - drains the outbox every few seconds (a tracked `process_outbox` run every ~10 minutes, like `process_outbox --loop`);
  - runs each scheduled command when its last finished `JobRun` is older than its interval, so a restart or a redeploy
    never repeats a job that has just run and never skips one that is due.
Jobs run one at a time in this process; a failing job is recorded by its own `JobRun` and the loop carries on.
"""
import logging
import signal
import time
from datetime import timedelta

from django.core.management import BaseCommand, call_command
from django.db import close_old_connections
from django.db.models import Max
from django.utils import timezone

from apps.core import outbox
from apps.core.models import JobRun

log = logging.getLogger("ndc.schedule")
M = timedelta(minutes=1)
H = timedelta(hours=1)
D = timedelta(days=1)

# (command, arguments, interval): the README's schedule (spec 11 onwards). The tracked name is the command's `job`.
SCHEDULE = [
    ("sweep_payments", [], 15 * M),
    ("moderation_tick", [], 15 * M),
    ("events_tick", [], 15 * M),
    ("privacy_tick", [], H),
    ("geo_notify_unconfirmed", [], H),  # spec 19 §12.2: 500 an hour, each record once
    ("check_donations", [], D),
    ("prune_pvc", [], D),
    ("prune_news", [], D),
    ("prune_notifications", [], D),
    ("prune_posts", [], D),
    ("prune_moderation", [], D),
    ("prune_communities", [], D),
    ("prune_events", [], D),
    ("prune_volunteers", [], D),
    ("prune_outbox", [], D),
    ("prune_webhooks", [], D),
    ("match_geography", [], D),  # spec 19 §12.2: before communities, so new exact links place people the same night
    ("sync_communities", ["--placements"], D),
    ("prune_jobruns", [], 7 * D),
    ("verify_audit", [], 7 * D),
    ("prune_audit", [], 7 * D),
    ("check_integrity", [], 7 * D),
    ("rebuild_search", [], 7 * D),
    ("media_audit", [], 7 * D),
]
OUTBOX_EVERY = 5  # seconds
OUTBOX_TRACK_EVERY = 10 * M


def due(now=None) -> list[tuple[str, list[str]]]:
    """Scheduled commands whose last finished run (ok or failed) is older than their interval, or that never ran."""
    now = now or timezone.now()
    last = dict(JobRun.objects.filter(finished_at__isnull=False).values("job").annotate(at=Max("finished_at"))
                .values_list("job", "at"))
    running = set(JobRun.objects.filter(finished_at__isnull=True, started_at__gt=now - 2 * H).values_list("job", flat=True))
    return [(cmd, args) for cmd, args, every in SCHEDULE
            if cmd not in running and (last.get(cmd) is None or now - last[cmd] >= every)]


class Command(BaseCommand):
    help = "Long-running worker: drains the outbox and runs every scheduled job when it is due."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run what is due once and exit (for checks).")

    def handle(self, *args, **opts):
        stop = {"now": False}

        def _stop(*_):  # Railway sends SIGTERM on redeploy: finish the current job, then exit
            stop["now"] = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        last_tracked = None
        tried: dict[str, object] = {}  # belt and braces: a command that fails before recording a JobRun waits its interval
        every = {cmd: e for cmd, _, e in SCHEDULE}
        while not stop["now"]:
            now = timezone.now()
            try:
                if last_tracked is None or now - last_tracked >= OUTBOX_TRACK_EVERY:
                    call_command("process_outbox")
                    last_tracked = now
                else:
                    outbox.process(limit=100)
            except Exception:  # noqa: BLE001 - the worker must keep going; process_outbox records its own failure
                log.exception("outbox run failed")
            for cmd, cmd_args in due(now):
                if stop["now"]:
                    break
                if cmd in tried and now - tried[cmd] < every[cmd]:
                    continue
                tried[cmd] = now
                try:
                    call_command(cmd, *cmd_args)
                except Exception:  # noqa: BLE001 - recorded in the job's own JobRun
                    log.exception("scheduled job %s failed", cmd)
            if opts.get("once"):
                return
            time.sleep(OUTBOX_EVERY)
            close_old_connections()  # drop a connection the database or the proxy closed while we slept
