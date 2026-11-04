import time

from apps.core import outbox
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    help = "Run due outbox jobs (spec 15 §9.3). Cron every minute, or --loop as a long-running worker."
    job = "process_outbox"

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, polling every few seconds.")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **opts):
        if not opts.get("loop"):
            return super().handle(*args, **opts)
        n = 0
        while True:  # pragma: no cover - long-running mode
            if n % 120 == 0:  # a tracked run every ~10 minutes, so the dashboard sees the worker alive
                super().handle(*args, **{**opts, "loop": False})
            else:
                outbox.process(limit=opts["limit"])
            n += 1
            time.sleep(5)

    def run(self, *args, limit=100, **opts):
        r = outbox.process(limit=limit)
        return f"{r['done']} done, {r['failed']} failed"
