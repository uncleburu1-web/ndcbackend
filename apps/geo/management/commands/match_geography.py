"""Spec 16 §8.2: link free-text LGA/ward/polling unit to the reference tables where the text matches exactly (after
folding). Never changes what a person typed. Counts only in the output."""
from django.core.management.base import BaseCommand

from apps.geo.matching import match_all


class Command(BaseCommand):
    help = "Match free-text LGA, ward and polling unit on memberships, PVC records and candidates to INEC's list."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report counts and write nothing.")

    def handle(self, *args, dry_run=False, **opts):
        for name, r in match_all(dry_run=dry_run).items():
            self.stdout.write(f"{name}: {r['checked']} checked, {r['lga']} LGA, {r['ward']} ward, {r['polling_unit']} polling unit "
                              f"matches{' (dry run)' if dry_run else ' linked'}")
