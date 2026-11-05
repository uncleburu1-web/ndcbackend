from django.core.management.base import CommandError

from apps.core import integrity
from apps.core.jobs import TrackedCommand


class Command(TrackedCommand):
    help = "Weekly data-integrity check (spec 16 §11.2): counts only. --constraint NAME lists the IDs breaking one rule."
    job = "check_integrity"

    def add_arguments(self, parser):
        parser.add_argument("--constraint", default="", help="List the IDs of rows that break this check constraint.")

    def handle(self, *args, **opts):
        if opts.get("constraint"):
            try:
                label, ids = integrity.violating_ids(opts["constraint"])
            except LookupError as e:
                raise CommandError(str(e)) from None
            self.stdout.write(f"{label}: {len(ids)} row(s) break {opts['constraint']}" + (":" if ids else "."))
            for i in ids:
                self.stdout.write(f"  {i}")
            return
        return super().handle(*args, **opts)

    def run(self, *args, **opts):
        r = integrity.run()
        for key, label, _ in integrity.CHECKS:
            n = r["counts"][key]
            self.stdout.write(f"{'ERROR' if n < 0 else 'OK   ' if n == 0 else 'ISSUE'} {label}: {'check failed' if n < 0 else n}")
        return integrity.summary(r)
