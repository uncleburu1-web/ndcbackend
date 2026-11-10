"""
python manage.py import_candidates [--file data/candidates/ndc_2027.json] [--dry-run] [--list]

Imports researched candidate profiles as drafts (see apps/candidates/importing.py). Safe to run again: it updates only
its own unreviewed drafts and never touches verified, published or hand-made records.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.candidates.importing import ImportFileError, load, run

DEFAULT = settings.BASE_DIR / "data" / "candidates" / "ndc_2027.json"


class Command(BaseCommand):
    help = "Import researched candidate profiles from a JSON file as drafts for admin review."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT), help="JSON file to import (default: %(default)s).")
        parser.add_argument("--dry-run", action="store_true", help="Check and report without saving anything.")
        parser.add_argument("--list", action="store_true", help="Also list every record by outcome.")

    def handle(self, *args, **opts):
        try:
            data = load(opts["file"])
            res = run(data, dry_run=opts["dry_run"])
        except ImportFileError as exc:
            raise CommandError("Import stopped, nothing saved:\n- " + "\n- ".join(exc.errors)) from exc
        if opts["list"]:
            for title, rows in (("Created", res.created), ("Updated", res.updated), ("Unchanged", res.unchanged),
                                ("Left alone (already reviewed)", res.kept_reviewed),
                                ("Left alone (already in the database)", res.kept_existing),
                                ("Flagged for careful review", res.flagged)):
                if rows:
                    self.stdout.write(f"{title}:")
                    for r in rows:
                        self.stdout.write(f"  - {r}")
        self.stdout.write(res.summary(dry_run=opts["dry_run"]))
