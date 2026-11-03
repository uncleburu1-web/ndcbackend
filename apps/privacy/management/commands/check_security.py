"""Spec 14 §4.10: one line per check (PASS/FAIL/WARN and a name), exit 1 on any FAIL. Never prints a value."""
import sys

from django.core.management.base import BaseCommand

from apps.privacy import checks


class Command(BaseCommand):
    help = "Check production security settings (names and results only)."

    def handle(self, *args, **opts):
        results = checks.run()
        for name, state in results:
            self.stdout.write(f"{state:<4} {name}")
        failed = [n for n, st in results if st == checks.FAIL]
        self.stdout.write(f"{len(failed)} failed, {sum(1 for _, st in results if st == checks.WARN)} warnings")
        if failed:
            sys.exit(1)
